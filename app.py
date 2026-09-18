"""API Flask - Pilotage des files de jeu (séance 1).

Un seul fichier, découpé en trois couches :
  - REPOSITORY : requêtes SQL (seule partie qui touche la base)
  - SERVICE    : validation des paramètres et règles métier
  - CONTROLLER : routes Flask et réponses HTTP
En séance 2, seule la partie REPOSITORY sera remplacée par SQLAlchemy.
"""

import logging
import os
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from flask import Blueprint, Flask, jsonify, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _trouver_base() -> Path:
    """Cherche parties.db : variable PARTIES_DB, sinon à côté de app.py, sinon dossier parent."""
    if os.environ.get("PARTIES_DB"):
        return Path(os.environ["PARTIES_DB"])
    ici = Path(__file__).resolve().parent
    for dossier in (ici, ici.parent):
        if (dossier / "parties.db").exists():
            return dossier / "parties.db"
    return ici / "parties.db"


DB_PATH = _trouver_base()

LIMIT_DEFAUT = 20
LIMIT_MAX = 100
TRI_DEFAUT = "date"
ORDRE_DEFAUT = "desc"

# Liste blanche : valeur reçue dans l'URL -> colonne SQL (jamais la saisie brute)
TRIS = {"date": "p.debut", "attente": "p.attente_secondes"}
ORDRES = {"asc": "ASC", "desc": "DESC"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Paramètre de requête invalide -> HTTP 400."""


# ---------------------------------------------------------------------------
# REPOSITORY : accès à la base (lecture seule)
# ---------------------------------------------------------------------------

def _connexion() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Base introuvable : {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


JOINTURES = """
    FROM parties p
    JOIN serveurs s ON s.id = p.serveur_id
    JOIN files fi   ON fi.id = p.file_id
    JOIN jeux j     ON j.id = fi.jeu_id
"""


def construire_filtres(filtres: dict) -> tuple[str, list]:
    """Clause WHERE commune à la liste, au total et (plus tard) aux agrégations."""
    conditions, params = [], []
    if filtres.get("annee") is not None:
        # debut est un texte 'AAAA-MM-JJ HH:MM:SS' : une plage utilise l'index
        conditions.append("p.debut >= ? AND p.debut < ?")
        params += [f"{filtres['annee']}-01-01", f"{filtres['annee'] + 1}-01-01"]
    if filtres.get("serveur_id") is not None:
        conditions.append("p.serveur_id = ?")
        params.append(filtres["serveur_id"])
    if filtres.get("jeu_id") is not None:
        conditions.append("fi.jeu_id = ?")
        params.append(filtres["jeu_id"])
    if filtres.get("file_id") is not None:
        conditions.append("p.file_id = ?")
        params.append(filtres["file_id"])
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def repo_compter_parties(filtres: dict) -> int:
    where, params = construire_filtres(filtres)
    with closing(_connexion()) as conn:
        return conn.execute(f"SELECT COUNT(*) {JOINTURES}{where}", params).fetchone()[0]


def repo_lister_parties(filtres: dict, tri: str, ordre: str, limit: int, offset: int) -> list[dict]:
    where, params = construire_filtres(filtres)
    colonne, sens = TRIS[tri], ORDRES[ordre]  # déjà validés par le service
    sql = f"""
        SELECT p.id, p.debut, p.attente_secondes, p.duree_minutes,
               s.code AS serveur, j.nom AS jeu, fi.nom AS file
        {JOINTURES}{where}
        ORDER BY {colonne} {sens}, p.id {sens}
        LIMIT ? OFFSET ?
    """
    with closing(_connexion()) as conn:
        lignes = conn.execute(sql, params + [limit, offset]).fetchall()
    return [dict(ligne) for ligne in lignes]


@lru_cache(maxsize=1)
def repo_referentiel() -> dict:
    """Serveurs, jeux, files et années. Mis en cache : la base est en lecture seule."""
    with closing(_connexion()) as conn:
        serveurs = [dict(r) for r in conn.execute(
            "SELECT id, code, nom, region FROM serveurs ORDER BY id")]
        jeux = [dict(r) for r in conn.execute("SELECT id, nom FROM jeux ORDER BY id")]
        files = [dict(r) for r in conn.execute(
            "SELECT id, nom, jeu_id FROM files ORDER BY jeu_id, id")]
        annees = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT substr(debut, 1, 4) FROM parties ORDER BY 1")]
    return {"annees": annees, "serveurs": serveurs, "jeux": jeux, "files": files}


# ---------------------------------------------------------------------------
# SERVICE : validation et règles métier
# ---------------------------------------------------------------------------

def _lire_entier(args, nom: str, defaut, minimum: int):
    brut = args.get(nom)
    if brut is None or brut == "":
        return defaut
    try:
        valeur = int(brut)
    except ValueError:
        raise ValidationError(f"Le paramètre '{nom}' doit être un entier (reçu : '{brut}').")
    if valeur < minimum:
        raise ValidationError(f"Le paramètre '{nom}' doit être supérieur ou égal à {minimum}.")
    return valeur


def _lire_choix(args, nom: str, defaut: str, autorises) -> str:
    valeur = (args.get(nom) or defaut).lower()
    if valeur not in autorises:
        raise ValidationError(
            f"Valeur '{args.get(nom)}' invalide pour '{nom}'. "
            f"Valeurs acceptées : {', '.join(autorises)}."
        )
    return valeur


def _resoudre_filtres(args) -> dict:
    """Transforme les libellés reçus (EUW, LoL, ARAM...) en identifiants validés.

    Comparaison insensible à la casse ; les accents font partie des noms.
    """
    ref = repo_referentiel()
    filtres = {"annee": None, "serveur_id": None, "jeu_id": None, "file_id": None}

    annee = _lire_entier(args, "annee", None, 0)
    if annee is not None and annee not in ref["annees"]:
        raise ValidationError(
            f"Année {annee} indisponible. Valeurs acceptées : "
            f"{', '.join(map(str, ref['annees']))}.")
    filtres["annee"] = annee

    serveur = args.get("serveur")
    if serveur:
        codes = {s["code"].casefold(): s["id"] for s in ref["serveurs"]}
        if serveur.casefold() not in codes:
            raise ValidationError(
                f"Serveur '{serveur}' inconnu. Valeurs acceptées : "
                f"{', '.join(s['code'] for s in ref['serveurs'])}.")
        filtres["serveur_id"] = codes[serveur.casefold()]

    jeu = args.get("jeu")
    if jeu:
        noms = {j["nom"].casefold(): j["id"] for j in ref["jeux"]}
        if jeu.casefold() not in noms:
            raise ValidationError(
                f"Jeu '{jeu}' inconnu. Valeurs acceptées : "
                f"{', '.join(j['nom'] for j in ref['jeux'])}.")
        filtres["jeu_id"] = noms[jeu.casefold()]

    file = args.get("file")
    if file:
        candidates = [f for f in ref["files"] if f["nom"].casefold() == file.casefold()]
        if filtres["jeu_id"] is not None:
            candidates = [f for f in candidates if f["jeu_id"] == filtres["jeu_id"]]
        if not candidates:
            possibles = [f["nom"] for f in ref["files"]
                         if filtres["jeu_id"] is None or f["jeu_id"] == filtres["jeu_id"]]
            raise ValidationError(
                f"File '{file}' inconnue pour ce filtre. Valeurs acceptées : "
                f"{', '.join(sorted(set(possibles)))}.")
        if len(candidates) > 1:
            raise ValidationError(
                f"La file '{file}' existe pour plusieurs jeux : précisez le paramètre 'jeu'.")
        filtres["file_id"] = candidates[0]["id"]

    return filtres


def _formater_partie(partie: dict) -> dict:
    """Date au format ISO 8601 ; duree_minutes reste null pour une partie en cours."""
    partie["debut"] = partie["debut"].replace(" ", "T")
    return partie


def service_lister_parties(args) -> dict:
    limit = _lire_entier(args, "limit", LIMIT_DEFAUT, 1)
    if limit > LIMIT_MAX:
        raise ValidationError(f"Le paramètre 'limit' ne peut pas dépasser {LIMIT_MAX}.")
    offset = _lire_entier(args, "offset", 0, 0)
    tri = _lire_choix(args, "tri", TRI_DEFAUT, TRIS)
    ordre = _lire_choix(args, "ordre", ORDRE_DEFAUT, ORDRES)
    filtres = _resoudre_filtres(args)

    total = repo_compter_parties(filtres)
    parties = repo_lister_parties(filtres, tri, ordre, limit, offset)
    return {
        "data": [_formater_partie(p) for p in parties],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def service_referentiel() -> dict:
    return repo_referentiel()


# ---------------------------------------------------------------------------
# CONTROLLER : routes Flask
# ---------------------------------------------------------------------------

parties_bp = Blueprint("parties", __name__, url_prefix="/api/v1/parties")


@parties_bp.get("")
def lister_parties():
    return jsonify(service_lister_parties(request.args)), 200


@parties_bp.get("/referentiel")
def referentiel():
    return jsonify(service_referentiel()), 200


def _erreur(code: str, message: str, statut: int):
    return jsonify({"error": {"code": code, "message": message}}), statut


def create_app() -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False   # accents lisibles dans le JSON
    app.json.sort_keys = False      # garde l'ordre data / total / limit / offset
    app.register_blueprint(parties_bp)

    @app.errorhandler(ValidationError)
    def erreur_validation(e):
        return _erreur("invalid_parameter", str(e), 400)

    @app.errorhandler(404)
    def introuvable(e):
        return _erreur("not_found", "Ressource introuvable.", 404)

    @app.errorhandler(405)
    def methode(e):
        return _erreur("method_not_allowed", "Méthode HTTP non autorisée.", 405)

    @app.errorhandler(Exception)
    def erreur_interne(e):
        logger.exception("Erreur interne")
        return _erreur("internal_error", "Erreur interne du serveur.", 500)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
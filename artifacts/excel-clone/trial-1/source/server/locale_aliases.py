"""Locale-aware function name aliases. Only the bits we need."""
from __future__ import annotations

# Each table maps local-name -> canonical (en-US) name. Stored upper-case.
# Translations cover the most common spreadsheet functions in each locale.

DE = {
    "SUMME":       "SUM",
    "MITTELWERT":  "AVERAGE",
    "WENN":        "IF",
    "ANZAHL":      "COUNT",
    "ANZAHL2":     "COUNTA",
    "MAX":         "MAX",
    "MIN":         "MIN",
    "PRODUKT":     "PRODUCT",
    "RUNDEN":      "ROUND",
    "WURZEL":      "SQRT",
    "POTENZ":      "POWER",
    "REST":        "MOD",
    "BETRAG":      "ABS",
    "GANZZAHL":    "INT",
    "ZÄHLENWENN":  "COUNTIF",
    "SUMMEWENN":   "SUMIF",
    "MITTELWERTWENN": "AVERAGEIF",
    "UND":         "AND",
    "ODER":        "OR",
    "NICHT":       "NOT",
    "WAHR":        "TRUE",
    "FALSCH":      "FALSE",
    "VERWEIS":     "LOOKUP",
    "SVERWEIS":    "VLOOKUP",
    "WVERWEIS":    "HLOOKUP",
    "VERGLEICH":   "MATCH",
    "INDEX":       "INDEX",
    "TEXT":        "TEXT",
    "LÄNGE":       "LEN",
    "LINKS":       "LEFT",
    "RECHTS":      "RIGHT",
    "TEIL":        "MID",
    "GROSS":       "UPPER",
    "KLEIN":       "LOWER",
    "GLÄTTEN":     "TRIM",
    "VERKETTEN":   "CONCAT",
    "WIEDERHOLEN": "REPT",
    "ERSETZEN":    "REPLACE",
    "WECHSELN":    "SUBSTITUTE",
    "DATUM":       "DATE",
    "JAHR":        "YEAR",
    "MONAT":       "MONTH",
    "TAG":         "DAY",
    "HEUTE":       "TODAY",
    "JETZT":       "NOW",
    "MEDIAN":      "MEDIAN",
    "STABW":       "STDEV",
    "STABWN":      "STDEVP",
    "VARIANZ":     "VAR",
    "VARIANZEN":   "VARP",
    "QUANTIL":     "PERCENTILE",
}

FR = {
    "SOMME":       "SUM",
    "MOYENNE":     "AVERAGE",
    "SI":          "IF",
    "NB":          "COUNT",
    "NBVAL":       "COUNTA",
    "MAX":         "MAX",
    "MIN":         "MIN",
    "PRODUIT":     "PRODUCT",
    "ARRONDI":     "ROUND",
    "RACINE":      "SQRT",
    "PUISSANCE":   "POWER",
    "MOD":         "MOD",
    "ABS":         "ABS",
    "ENT":         "INT",
    "NB.SI":       "COUNTIF",
    "SOMME.SI":    "SUMIF",
    "MOYENNE.SI":  "AVERAGEIF",
    "ET":          "AND",
    "OU":          "OR",
    "NON":         "NOT",
    "VRAI":        "TRUE",
    "FAUX":        "FALSE",
    "RECHERCHE":   "LOOKUP",
    "RECHERCHEV":  "VLOOKUP",
    "RECHERCHEH":  "HLOOKUP",
    "EQUIV":       "MATCH",
    "INDEX":       "INDEX",
    "TEXTE":       "TEXT",
    "NBCAR":       "LEN",
    "GAUCHE":      "LEFT",
    "DROITE":      "RIGHT",
    "STXT":        "MID",
    "MAJUSCULE":   "UPPER",
    "MINUSCULE":   "LOWER",
    "SUPPRESPACE": "TRIM",
    "CONCAT":      "CONCAT",
    "REPT":        "REPT",
    "REMPLACER":   "REPLACE",
    "SUBSTITUE":   "SUBSTITUTE",
    "DATE":        "DATE",
    "ANNEE":       "YEAR",
    "MOIS":        "MONTH",
    "JOUR":        "DAY",
    "AUJOURDHUI":  "TODAY",
    "MAINTENANT":  "NOW",
    "MEDIANE":     "MEDIAN",
    "ECARTYPE":    "STDEV",
    "ECARTYPEP":   "STDEVP",
    "VAR":         "VAR",
    "VARP":        "VARP",
    "CENTILE":     "PERCENTILE",
}

ES = {
    "SUMA":        "SUM",
    "PROMEDIO":    "AVERAGE",
    "SI":          "IF",
    "CONTAR":      "COUNT",
    "CONTARA":     "COUNTA",
    "MAX":         "MAX",
    "MIN":         "MIN",
    "PRODUCTO":    "PRODUCT",
    "REDONDEAR":   "ROUND",
    "RAIZ":        "SQRT",
    "POTENCIA":    "POWER",
    "RESIDUO":     "MOD",
    "ABS":         "ABS",
    "ENTERO":      "INT",
    "CONTAR.SI":   "COUNTIF",
    "SUMAR.SI":    "SUMIF",
    "PROMEDIO.SI": "AVERAGEIF",
    "Y":           "AND",
    "O":           "OR",
    "NO":          "NOT",
    "VERDADERO":   "TRUE",
    "FALSO":       "FALSE",
    "BUSCAR":      "LOOKUP",
    "BUSCARV":     "VLOOKUP",
    "BUSCARH":     "HLOOKUP",
    "COINCIDIR":   "MATCH",
    "INDICE":      "INDEX",
    "TEXTO":       "TEXT",
    "LARGO":       "LEN",
    "IZQUIERDA":   "LEFT",
    "DERECHA":     "RIGHT",
    "EXTRAE":      "MID",
    "MAYUSC":      "UPPER",
    "MINUSC":      "LOWER",
    "ESPACIOS":    "TRIM",
    "CONCAT":      "CONCAT",
    "REPETIR":     "REPT",
    "REEMPLAZAR":  "REPLACE",
    "SUSTITUIR":   "SUBSTITUTE",
    "FECHA":       "DATE",
    "AÑO":         "YEAR",
    "MES":         "MONTH",
    "DIA":         "DAY",
    "HOY":         "TODAY",
    "AHORA":       "NOW",
    "MEDIANA":     "MEDIAN",
    "DESVEST":     "STDEV",
    "DESVESTP":    "STDEVP",
    "VAR":         "VAR",
    "VARP":        "VARP",
    "PERCENTIL":   "PERCENTILE",
}


def locale_table(locale: str | None):
    if not locale:
        return None
    base = locale.split("-")[0].lower()
    return {"de": DE, "fr": FR, "es": ES}.get(base)


def arg_separator(locale: str | None) -> str:
    return ";" if locale and locale.split("-")[0].lower() in ("de", "fr", "es") else ","


def decimal_separator(locale: str | None) -> str:
    return "," if locale and locale.split("-")[0].lower() in ("de", "fr", "es") else "."


def canonicalize_call(name: str, locale: str | None) -> str:
    if not locale:
        return name
    table = locale_table(locale)
    if not table:
        return name
    upper = name.upper()
    return table.get(upper, name)

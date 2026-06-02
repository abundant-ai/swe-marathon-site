"""Locale function name mapping (translates non-EN names to EN canonical)."""
LOCALES = {
    'de-DE': {
        'SUMME': 'SUM', 'MITTELWERT': 'AVERAGE', 'ANZAHL': 'COUNT',
        'MAX': 'MAX', 'MIN': 'MIN', 'WENN': 'IF', 'UND': 'AND', 'ODER': 'OR',
        'NICHT': 'NOT', 'WAHR': 'TRUE', 'FALSCH': 'FALSE',
        'PRODUKT': 'PRODUCT', 'RUNDEN': 'ROUND', 'WURZEL': 'SQRT',
        'POTENZ': 'POWER', 'ABS': 'ABS', 'GANZZAHL': 'INT',
        'HEUTE': 'TODAY', 'JETZT': 'NOW',
        'ANZAHL2': 'COUNTA', 'SUMMEWENN': 'SUMIF', 'ZÄHLENWENN': 'COUNTIF',
        'SVERWEIS': 'VLOOKUP', 'WVERWEIS': 'HLOOKUP', 'INDEX': 'INDEX', 'VERGLEICH': 'MATCH',
        'LÄNGE': 'LEN', 'LINKS': 'LEFT', 'RECHTS': 'RIGHT', 'TEIL': 'MID',
        'GROSS': 'UPPER', 'KLEIN': 'LOWER', 'GLÄTTEN': 'TRIM', 'VERKETTEN': 'CONCATENATE',
    },
    'fr-FR': {
        'SOMME': 'SUM', 'MOYENNE': 'AVERAGE', 'NB': 'COUNT', 'NBVAL': 'COUNTA',
        'MAX': 'MAX', 'MIN': 'MIN', 'SI': 'IF', 'ET': 'AND', 'OU': 'OR',
        'NON': 'NOT', 'VRAI': 'TRUE', 'FAUX': 'FALSE',
        'PRODUIT': 'PRODUCT', 'ARRONDI': 'ROUND', 'RACINE': 'SQRT',
        'PUISSANCE': 'POWER', 'ABS': 'ABS', 'ENT': 'INT',
        'AUJOURDHUI': 'TODAY', 'MAINTENANT': 'NOW',
        'SOMME.SI': 'SUMIF', 'NB.SI': 'COUNTIF',
        'RECHERCHEV': 'VLOOKUP', 'RECHERCHEH': 'HLOOKUP', 'INDEX': 'INDEX', 'EQUIV': 'MATCH',
        'NBCAR': 'LEN', 'GAUCHE': 'LEFT', 'DROITE': 'RIGHT', 'STXT': 'MID',
        'MAJUSCULE': 'UPPER', 'MINUSCULE': 'LOWER', 'SUPPRESPACE': 'TRIM', 'CONCATENER': 'CONCATENATE',
    },
    'es-ES': {
        'SUMA': 'SUM', 'PROMEDIO': 'AVERAGE', 'CONTAR': 'COUNT', 'CONTARA': 'COUNTA',
        'MAX': 'MAX', 'MIN': 'MIN', 'SI': 'IF', 'Y': 'AND', 'O': 'OR',
        'NO': 'NOT', 'VERDADERO': 'TRUE', 'FALSO': 'FALSE',
        'PRODUCTO': 'PRODUCT', 'REDONDEAR': 'ROUND', 'RAIZ': 'SQRT',
        'POTENCIA': 'POWER', 'ABS': 'ABS', 'ENTERO': 'INT',
        'HOY': 'TODAY', 'AHORA': 'NOW',
        'SUMAR.SI': 'SUMIF', 'CONTAR.SI': 'COUNTIF',
        'BUSCARV': 'VLOOKUP', 'BUSCARH': 'HLOOKUP', 'INDICE': 'INDEX', 'COINCIDIR': 'MATCH',
        'LARGO': 'LEN', 'IZQUIERDA': 'LEFT', 'DERECHA': 'RIGHT', 'EXTRAE': 'MID',
        'MAYUSC': 'UPPER', 'MINUSC': 'LOWER', 'ESPACIOS': 'TRIM', 'CONCATENAR': 'CONCATENATE',
    },
}

def translate_formula(text, locale):
    """Translate locale-specific formula text to en-US canonical.
    - Function names mapped from LOCALES table.
    - For de/fr/es: arg sep ';' -> ',', decimal ',' -> '.' inside numbers.
    Operates outside string literals.
    """
    if not locale or locale not in LOCALES:
        return text
    mapping = LOCALES[locale]
    out = []
    i = 0
    in_str = False
    while i < len(text):
        c = text[i]
        if c == '"':
            in_str = not in_str; out.append(c); i += 1; continue
        if in_str:
            out.append(c); i += 1; continue
        # identifier
        if c.isalpha() or c == '_':
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] in '_.äöüÄÖÜßéèêàùîïôçñ'):
                j += 1
            ident = text[i:j]
            up = ident.upper()
            if up in mapping:
                out.append(mapping[up])
            else:
                out.append(ident)
            i = j
            continue
        # number with comma decimal: digit (,digit)
        if c.isdigit():
            j = i
            while j < len(text) and text[j].isdigit(): j += 1
            if j < len(text) and text[j] == ',' and j+1 < len(text) and text[j+1].isdigit():
                # decimal comma - replace with .
                out.append(text[i:j]); out.append('.'); i = j+1; continue
            out.append(text[i:j]); i = j; continue
        if c == ';':
            out.append(','); i += 1; continue
        out.append(c); i += 1
    return ''.join(out)

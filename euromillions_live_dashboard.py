def parse_official_xml(text: str) -> pd.DataFrame:
    import xml.etree.ElementTree as ET
    import re

    root = ET.fromstring(text)
    rows = []

    for draw in root.iter():
        tag = draw.tag.lower()

        # prendiamo qualsiasi elemento che contiene 'draw'
        if "draw" not in tag:
            continue

        values = []

        for child in draw.iter():
            if child.text:
                t = child.text.strip()
                if re.fullmatch(r"\d{1,2}", t):
                    values.append(int(t))

        # se troviamo almeno 7 numeri -> è una draw valida
        if len(values) >= 7:
            balls = sorted(values[:5])
            stars = sorted(values[5:7])

            row = {
                "draw_date": None,
                "source": "official_xml"
            }

            # prova a trovare la data
            for child in draw.iter():
                if child.text:
                    txt = child.text.strip()
                    if "-" in txt and len(txt) >= 8:
                        row["draw_date"] = txt
                        break

            # fallback se non trova data
            if not row["draw_date"]:
                continue

            for i, v in enumerate(balls, 1):
                row[f"ball_{i}"] = v

            row["lucky_star_1"] = stars[0]
            row["lucky_star_2"] = stars[1]

            rows.append(row)

    if not rows:
        raise ValueError("No draw rows parsed from official XML.")

    df = pd.DataFrame(rows)
    return standardize_columns(df)
"""Deterministic, paginated Pillow tables from saved official contest snapshots."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PAGE_SIZE = 40


def report_row_cells(row, sequence):
    solved = "—" if row["solved"] is None else str(row["solved"])
    if row["rank"] is None:
        return ["—", "—", row["handle"], solved, "—", "—"]
    delta = row["rating_change"]
    return [str(sequence), str(row["rank"]), row["handle"], solved,
            "—" if delta is None else f"{delta:+d}" if delta else "0",
            "—" if row["new_rating"] is None else str(row["new_rating"])]


def rating_color(rating):
    if rating is None or rating < 1200:
        return "#737b89"
    if rating < 1400:
        return "#008000"
    if rating < 1600:
        return "#039b92"
    if rating < 1900:
        return "#2457c5"
    if rating < 2100:
        return "#a000a0"
    if rating < 2400:
        return "#d77900"
    return "#df303e"


def _wrap(text, font, width):
    lines, current = [], ""
    for char in text:
        if char == "\n" or (current and font.getlength(current + char) > width):
            lines.append(current.rstrip())
            current = "" if char == "\n" else char
        else:
            current += char
    if current:
        lines.append(current.rstrip())
    return lines or [""]


def render_report_pages(report, font_path, page_size=PAGE_SIZE):
    if not report["rows"]:
        return []
    if page_size < 1:
        raise ValueError("page_size must be positive")
    font_path = str(Path(font_path))
    title_font = ImageFont.truetype(font_path, 34)
    header_font = ImageFont.truetype(font_path, 21)
    body_font = ImageFont.truetype(font_path, 26)
    small_font = ImageFont.truetype(font_path, 17)
    width, margin = 1160, 34
    columns = [64, 100, 332, 130, 242, 224]
    headers = ["", "Rank", "Handle", "Solved", "Rating change", "New rating"]
    title_lines = _wrap(report["contest_name"], title_font, width - 2 * margin - 40)
    title_height = 40 + 47 * len(title_lines)
    header_height, row_height, footer_height = 56, 70, 44
    total_pages = (len(report["rows"]) + page_size - 1) // page_size
    pages = []
    for start in range(0, len(report["rows"]), page_size):
        rows = report["rows"][start:start + page_size]
        height = margin + title_height + header_height + len(rows) * row_height + footer_height + margin
        image = Image.new("RGB", (width, height), "#edf2f8")
        draw = ImageDraw.Draw(image)
        right = width - margin
        draw.rounded_rectangle((margin, margin, right, height-margin), radius=18, fill="#ffffff")
        draw.rounded_rectangle((margin, margin, right, margin+title_height+18), radius=18, fill="#172b47")
        draw.rectangle((margin, margin+title_height-3, right, margin+title_height+18), fill="#172b47")
        for line, text in enumerate(title_lines):
            draw.text((width/2, margin+23+47*line), text, font=title_font, fill="#ffffff", anchor="mt")
        header_y = margin + title_height
        draw.rectangle((margin, header_y, right, header_y+header_height), fill="#e8eef7")
        left = margin
        for label, col_width in zip(headers, columns):
            draw.text((left+col_width/2, header_y+header_height/2), label, font=header_font,
                      fill="#425775", anchor="mm")
            left += col_width
        for index, row in enumerate(rows):
            top = header_y + header_height + index * row_height
            if index % 2:
                draw.rectangle((margin, top, right, top+row_height), fill="#f6f8fc")
            draw.line((margin+16, top+row_height, right-16, top+row_height), fill="#e3eaf2", width=1)
            delta = row["rating_change"]
            values = report_row_cells(row, start+index+1)
            left = margin
            for col, (text, col_width) in enumerate(zip(values, columns)):
                color = "#26384f"
                if col == 0:
                    color = "#8793a4"
                elif col == 2:
                    color = rating_color(row.get("display_rating", row["new_rating"]))
                elif col == 5:
                    color = rating_color(row["new_rating"])
                elif col == 4 and delta:
                    color = "#15925c" if delta > 0 else "#da4251"
                font = body_font
                while font.getlength(text) > col_width-20 and font.size > 15:
                    font = ImageFont.truetype(font_path, font.size-1)
                center = top + row_height/2
                handle_rating = row.get("display_rating", row["new_rating"])
                if col == 2 and handle_rating is not None and handle_rating >= 3000:
                    x = left + (col_width-font.getlength(text))/2
                    draw.text((x, center), text[0], font=font, fill="#172b47", anchor="lm")
                    draw.text((x+font.getlength(text[0]), center), text[1:], font=font, fill=color, anchor="lm")
                else:
                    draw.text((left+col_width/2, center), text, font=font, fill=color, anchor="mm")
                left += col_width
        footer = f"Codeforces  ·  #{report['contest_id']}"
        draw.text((margin+18, height-margin-footer_height/2), footer, font=small_font, fill="#8090a6", anchor="lm")
        if total_pages > 1:
            draw.text((right-18, height-margin-footer_height/2), f"{start//page_size+1} / {total_pages}",
                      font=small_font, fill="#8090a6", anchor="rm")
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        pages.append(buffer.getvalue())
    return pages

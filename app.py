import base64
import csv
import json
import re
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path

import fitz
import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import joinedload
from sqlalchemy import text

from models import (
    Anmeldung,
    Altersklasse,
    AppMeta,
    Base,
    Bewerb,
    Jahrgang,
    Lauf,
    LaufBahn,
    SessionLocal,
    Teilnehmer,
    assign_bewerbe_for_teilnehmer,
    bewerb_has_times,
    create_sample_data,
    engine,
    format_ms,
    generate_runs_for_bewerb,
    is_staffel_bewerb,
    normalized_gender,
    parse_bool,
    parse_time_to_ms,
)


BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "logo.png"


st.set_page_config(
    page_title="Ortsmeisterschaften",
    page_icon="🏊",
    layout="wide",
    initial_sidebar_state="expanded",
)


def init_database():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        bewerb_columns = connection.execute(text("PRAGMA table_info(bewerb)")).fetchall()
        bewerb_column_names = {column[1] for column in bewerb_columns}
        if "ortsmeister_relevant" not in bewerb_column_names:
            connection.execute(text("ALTER TABLE bewerb ADD COLUMN ortsmeister_relevant BOOLEAN DEFAULT 0"))
        if "ortsmeister_maennlich" not in bewerb_column_names:
            connection.execute(text("ALTER TABLE bewerb ADD COLUMN ortsmeister_maennlich BOOLEAN DEFAULT 0"))
        if "ortsmeister_weiblich" not in bewerb_column_names:
            connection.execute(text("ALTER TABLE bewerb ADD COLUMN ortsmeister_weiblich BOOLEAN DEFAULT 0"))
        teilnehmer_columns = connection.execute(text("PRAGMA table_info(teilnehmer)")).fetchall()
        teilnehmer_column_names = {column[1] for column in teilnehmer_columns}
        if "gast" not in teilnehmer_column_names:
            connection.execute(text("ALTER TABLE teilnehmer ADD COLUMN gast BOOLEAN DEFAULT 0"))
        altersklasse_columns = connection.execute(text("PRAGMA table_info(altersklasse)")).fetchall()
        altersklasse_column_names = {column[1] for column in altersklasse_columns}
        if altersklasse_columns and "distanz" not in altersklasse_column_names:
            connection.execute(text("ALTER TABLE altersklasse ADD COLUMN distanz VARCHAR(50) DEFAULT ''"))
        if altersklasse_columns and "sortierung" not in altersklasse_column_names:
            connection.execute(text("ALTER TABLE altersklasse ADD COLUMN sortierung INTEGER DEFAULT 0"))
    create_sample_data()
    create_default_altersklassen()


def get_db():
    return SessionLocal()


def create_default_altersklassen():
    db = get_db()
    try:
        if db.query(Altersklasse).first():
            return
        defaults = [
            ("Kinder", "25", 2018, 2100),
            ("Kinder", "25", 2017, 2017),
            ("Kinder", "25", 2016, 2016),
            ("Jugend", "50", 2015, 2015),
            ("Jugend", "50", 2014, 2014),
            ("Jugend", "50", 2013, 2013),
            ("Jugend", "50", 2012, 2012),
            ("Jugend", "50", 2011, 2011),
            ("Allgemeine Klasse", "100", 2001, 2010),
            ("Altersklasse I", "", 1991, 2000),
            ("Altersklasse II", "", 1981, 1990),
            ("Altersklasse III", "", 1971, 1980),
            ("Altersklasse IV", "", 1951, 1970),
            ("Altersklasse V", "", 0, 1950),
        ]
        for index, (name, distanz, jahr_von, jahr_bis) in enumerate(defaults, start=1):
            db.add(
                Altersklasse(
                    name=name,
                    distanz=distanz,
                    jahr_von=jahr_von,
                    jahr_bis=jahr_bis,
                    sortierung=index,
                )
            )
        db.commit()
    finally:
        db.close()


def refresh():
    st.rerun()


def draw_logo(canvas, x_mm, y_mm, width_mm, height_mm):
    if not LOGO_PATH.exists():
        return
    canvas.drawImage(
        ImageReader(str(LOGO_PATH)),
        x_mm * mm,
        y_mm * mm,
        width=width_mm * mm,
        height=height_mm * mm,
        preserveAspectRatio=True,
        mask="auto",
    )


def format_time_input(ms):
    if not ms:
        return ""
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    hundredths = (ms % 1000) // 10
    return f"{minutes:02d}:{seconds:02d}:{hundredths:02d}"


def normalize_time_input(key):
    ms = parse_time_to_ms(st.session_state.get(key, ""))
    st.session_state[key] = format_time_input(ms)


def center_certificate_field(prefix, axis):
    if axis in {"x", "both"}:
        st.session_state[f"{prefix}_x"] = 105.0
    if axis in {"y", "both"}:
        st.session_state[f"{prefix}_y"] = 148.5


CERTIFICATE_TEMPLATE_KEY = "certificate_template"


def certificate_field_defaults():
    return {
        "Name": {"prefix": "name", "x": 105.0, "y": 118.0, "size": 24},
        "Bewerb": {"prefix": "competition", "x": 105.0, "y": 142.0, "size": 14},
        "Altersklasse": {"prefix": "age_class", "x": 105.0, "y": 156.0, "size": 14},
        "Platz": {"prefix": "place", "x": 105.0, "y": 174.0, "size": 22},
        "Zeit": {"prefix": "time", "x": 105.0, "y": 193.0, "size": 16},
        "Datum": {"prefix": "date", "x": 105.0, "y": 230.0, "size": 12},
    }


def load_certificate_template(db):
    meta = db.query(AppMeta).get(CERTIFICATE_TEMPLATE_KEY)
    if not meta:
        return {}
    try:
        return json.loads(meta.value)
    except json.JSONDecodeError:
        return {}


def save_certificate_template(db, settings):
    value = json.dumps(settings, ensure_ascii=False)
    meta = db.query(AppMeta).get(CERTIFICATE_TEMPLATE_KEY)
    if meta:
        meta.value = value
    else:
        db.add(AppMeta(key=CERTIFICATE_TEMPLATE_KEY, value=value))
    db.commit()


def initialize_certificate_template_state(db, defaults):
    if st.session_state.get("certificate_template_initialized"):
        return
    template = load_certificate_template(db)
    for config in defaults.values():
        prefix = config["prefix"]
        st.session_state[f"{prefix}_x"] = float(template.get(f"{prefix}_x", config["x"]))
        st.session_state[f"{prefix}_y"] = float(template.get(f"{prefix}_y", config["y"]))
        st.session_state[f"{prefix}_size"] = int(template.get(f"{prefix}_size", config["size"]))
        st.session_state[f"{prefix}_visible"] = bool(template.get(f"{prefix}_visible", True))
    st.session_state["certificate_date_text"] = template.get(
        "date_text",
        f"Vorchdorf, {datetime.now().strftime('%d.%m.%Y')}",
    )
    st.session_state["certificate_template_initialized"] = True


def collect_certificate_template_settings(defaults):
    settings = {}
    for config in defaults.values():
        prefix = config["prefix"]
        settings[f"{prefix}_x"] = st.session_state[f"{prefix}_x"]
        settings[f"{prefix}_y"] = st.session_state[f"{prefix}_y"]
        settings[f"{prefix}_size"] = st.session_state[f"{prefix}_size"]
        settings[f"{prefix}_visible"] = st.session_state[f"{prefix}_visible"]
    settings["date_text"] = st.session_state.get("certificate_date_text", "")
    return settings


def apply_certificate_template_to_session(template, defaults):
    for config in defaults.values():
        prefix = config["prefix"]
        st.session_state[f"{prefix}_x"] = float(template.get(f"{prefix}_x", config["x"]))
        st.session_state[f"{prefix}_y"] = float(template.get(f"{prefix}_y", config["y"]))
        st.session_state[f"{prefix}_size"] = int(template.get(f"{prefix}_size", config["size"]))
        st.session_state[f"{prefix}_visible"] = bool(template.get(f"{prefix}_visible", True))
    st.session_state["certificate_date_text"] = template.get(
        "date_text",
        f"Vorchdorf, {datetime.now().strftime('%d.%m.%Y')}",
    )


def apply_pending_certificate_template(defaults):
    pending_template = st.session_state.pop("pending_certificate_template", None)
    if pending_template is not None:
        apply_certificate_template_to_session(pending_template, defaults)


def save_lane_time(lane_id, key):
    ms = parse_time_to_ms(st.session_state.get(key, ""))
    st.session_state[key] = format_time_input(ms)
    db = get_db()
    try:
        lane = db.query(LaufBahn).get(lane_id)
        if lane:
            lane.zeit_ms = ms
            db.commit()
    finally:
        db.close()


def save_ortsmeister_flag(bewerb_id, key):
    db = get_db()
    try:
        bewerb = db.query(Bewerb).get(bewerb_id)
        if not bewerb:
            return
        checked = bool(st.session_state.get(key, False))
        bewerb.ortsmeister_relevant = checked
        bewerb.ortsmeister_maennlich = checked
        bewerb.ortsmeister_weiblich = checked
        db.commit()
    finally:
        db.close()


def export_backup(db):
    data = {
        "jahrgaenge": [
            {"id": item.id, "name": item.name, "jahr_von": item.jahr_von, "jahr_bis": item.jahr_bis}
            for item in db.query(Jahrgang).order_by(Jahrgang.id).all()
        ],
        "altersklassen": [
            {
                "id": item.id,
                "name": item.name,
                "distanz": item.distanz or "",
                "jahr_von": item.jahr_von,
                "jahr_bis": item.jahr_bis,
                "sortierung": item.sortierung or 0,
            }
            for item in db.query(Altersklasse).order_by(Altersklasse.sortierung, Altersklasse.id).all()
        ],
        "certificate_template": load_certificate_template(db),
        "bewerbe": [
            {
                "id": item.id,
                "name": item.name,
                "stil": item.stil,
                "geschlecht": item.geschlecht,
                "distanz": item.distanz,
                "ortsmeister_relevant": bool(item.ortsmeister_relevant),
                "ortsmeister_maennlich": bool(item.ortsmeister_maennlich),
                "ortsmeister_weiblich": bool(item.ortsmeister_weiblich),
                "jahrgang_id": item.jahrgang_id,
            }
            for item in db.query(Bewerb).order_by(Bewerb.id).all()
        ],
        "teilnehmer": [
            {
                "id": item.id,
                "vorname": item.vorname,
                "nachname": item.nachname,
                "geburtsjahr": item.geburtsjahr,
                "geschlecht": item.geschlecht,
                "brust": bool(item.brust),
                "freistil": bool(item.freistil),
                "gast": bool(item.gast),
                "staffel": item.staffel or "",
            }
            for item in db.query(Teilnehmer).order_by(Teilnehmer.id).all()
        ],
        "anmeldungen": [
            {"id": item.id, "teilnehmer_id": item.teilnehmer_id, "bewerb_id": item.bewerb_id}
            for item in db.query(Anmeldung).order_by(Anmeldung.id).all()
        ],
        "laufe": [
            {
                "id": item.id,
                "bewerb_id": item.bewerb_id,
                "laufnummer": item.laufnummer,
                "status": item.status,
            }
            for item in db.query(Lauf).order_by(Lauf.id).all()
        ],
        "laufbahnen": [
            {
                "id": item.id,
                "lauf_id": item.lauf_id,
                "bahn": item.bahn,
                "teilnehmer_id": item.teilnehmer_id,
                "zeit_ms": item.zeit_ms or 0,
            }
            for item in db.query(LaufBahn).order_by(LaufBahn.id).all()
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def restore_backup(db, data):
    for model in [LaufBahn, Lauf, Anmeldung, Teilnehmer, Bewerb, Jahrgang, Altersklasse]:
        db.query(model).delete(synchronize_session=False)
    db.flush()

    for item in data.get("jahrgaenge", []):
        db.add(Jahrgang(**item))
    db.flush()
    for index, item in enumerate(data.get("altersklassen", []), start=1):
        item.setdefault("distanz", "")
        item.setdefault("sortierung", index)
        db.add(Altersklasse(**item))
    db.flush()
    for item in data.get("bewerbe", []):
        item.setdefault("ortsmeister_relevant", False)
        item.setdefault("ortsmeister_maennlich", item.get("ortsmeister_relevant", False))
        item.setdefault("ortsmeister_weiblich", item.get("ortsmeister_relevant", False))
        db.add(Bewerb(**item))
    db.flush()
    for item in data.get("teilnehmer", []):
        item.setdefault("gast", False)
        db.add(Teilnehmer(**item))
    db.flush()
    for item in data.get("anmeldungen", []):
        db.add(Anmeldung(**item))
    db.flush()
    for item in data.get("laufe", []):
        db.add(Lauf(**item))
    db.flush()
    for item in data.get("laufbahnen", []):
        db.add(LaufBahn(**item))
    db.commit()
    if data.get("certificate_template"):
        save_certificate_template(db, data["certificate_template"])
    if not data.get("altersklassen"):
        create_default_altersklassen()


def reset_championship(db):
    for model in [LaufBahn, Lauf, Anmeldung, Teilnehmer, Bewerb, Jahrgang, Altersklasse]:
        db.query(model).delete(synchronize_session=False)
    db.commit()
    create_default_altersklassen()


def clear_reset_checkboxes():
    st.session_state["reset_form_version"] = st.session_state.get("reset_form_version", 0) + 1


def update_assignments_for_participant(participant, db):
    db.query(Anmeldung).filter_by(teilnehmer_id=participant.id).delete(synchronize_session=False)
    db.flush()
    assign_bewerbe_for_teilnehmer(participant, db)


def update_assignments_for_all_participants(db):
    participants = db.query(Teilnehmer).all()
    for participant in participants:
        update_assignments_for_participant(participant, db)


def get_or_create_relay_jahrgang(db):
    jahrgang = db.query(Jahrgang).filter_by(name="Staffel").first()
    if jahrgang:
        return jahrgang
    jahrgang = Jahrgang(name="Staffel", jahr_von=0, jahr_bis=2100)
    db.add(jahrgang)
    db.commit()
    return jahrgang


def delete_participant(participant, db):
    db.query(LaufBahn).filter_by(teilnehmer_id=participant.id).update(
        {LaufBahn.teilnehmer_id: None, LaufBahn.zeit_ms: 0},
        synchronize_session=False,
    )
    db.query(Anmeldung).filter_by(teilnehmer_id=participant.id).delete(synchronize_session=False)
    db.delete(participant)
    db.commit()


def participant_rows(participants):
    return [
        {
            "ID": participant.id,
            "Name": participant.display_name(),
            "Vorname": participant.vorname,
            "Nachname": participant.nachname,
            "Geburtsjahr": participant.geburtsjahr,
            "Geschlecht": participant.geschlecht,
            "Brust": bool(participant.brust),
            "Freistil": bool(participant.freistil),
            "Gast": bool(participant.gast),
            "Staffel": participant.staffel or "-",
            "Zugeordnete Bewerbe": ", ".join(
                anmeldung.bewerb.full_name()
                for anmeldung in sorted(participant.anmeldungen, key=lambda item: item.bewerb.id)
            )
            or "-",
            "Loeschen": False,
        }
        for participant in participants
    ]


def global_run_numbers(bewerbe):
    runs = [
        lauf
        for bewerb in sorted(bewerbe, key=lambda item: item.id)
        for lauf in sorted(bewerb.laufe, key=lambda item: (item.laufnummer, item.id))
    ]
    return {lauf.id: number for number, lauf in enumerate(runs, start=1)}


def altersklasse_for_birth_year(altersklassen, geburtsjahr):
    for altersklasse in altersklassen:
        if altersklasse.jahr_von <= geburtsjahr <= altersklasse.jahr_bis:
            return altersklasse
    return None


def normalize_distance_number(value):
    return re.sub(r"\D", "", str(value or ""))


def format_distance_m(value):
    distance = normalize_distance_number(value)
    return f"{distance} m" if distance else ""


def altersklasse_label(altersklasse):
    if not altersklasse:
        return "Ohne Altersklasse"
    distanz = f" - {format_distance_m(altersklasse.distanz)}" if altersklasse.distanz else ""
    return f"{altersklasse.name}{distanz} ({altersklasse.display_range()})"


def result_group_label(bewerb, group):
    if is_staffel_bewerb(bewerb):
        return "Staffel"
    return altersklasse_label(group["altersklasse"])


def group_results_by_altersklasse(lane_results, altersklassen):
    grouped = []
    groups_by_id = {}
    fallback = {"altersklasse": None, "results": [], "sortierung": 9999}

    for item in lane_results:
        teilnehmer = item["teilnehmer"]
        altersklasse = altersklasse_for_birth_year(altersklassen, teilnehmer.geburtsjahr)
        item["altersklasse"] = altersklasse
        if altersklasse:
            group = groups_by_id.setdefault(
                altersklasse.id,
                {"altersklasse": altersklasse, "results": [], "sortierung": altersklasse.sortierung or 0},
            )
        else:
            group = fallback
        group["results"].append(item)

    for group in groups_by_id.values():
        group["results"].sort(key=lambda item: (item["zeit_ms"], item["teilnehmer"].id))
        grouped.append(group)
    if fallback["results"]:
        fallback["results"].sort(key=lambda item: (item["zeit_ms"], item["teilnehmer"].id))
        grouped.append(fallback)
    grouped.sort(key=lambda group: (group["sortierung"], altersklasse_label(group["altersklasse"])))
    return grouped


def build_results(db):
    results = []
    altersklassen = db.query(Altersklasse).order_by(Altersklasse.sortierung, Altersklasse.id).all()
    bewerbe = (
        db.query(Bewerb)
        .options(
            joinedload(Bewerb.jahrgang),
            joinedload(Bewerb.laufe)
            .joinedload(Lauf.laufbahnen)
            .joinedload(LaufBahn.teilnehmer),
        )
        .order_by(Bewerb.id)
        .all()
    )
    for bewerb in bewerbe:
        lane_results = []
        for lauf in sorted(bewerb.laufe, key=lambda item: item.laufnummer):
            for bahn in sorted(lauf.laufbahnen, key=lambda item: item.bahn):
                if bahn.teilnehmer_id and bahn.zeit_ms:
                    lane_results.append(
                        {
                            "bewerb": bewerb,
                            "lauf": lauf,
                            "bahn": bahn,
                            "zeit_ms": bahn.zeit_ms,
                            "teilnehmer": bahn.teilnehmer,
                        }
                    )
        lane_results.sort(key=lambda item: item["zeit_ms"])
        result_groups = (
            [{"altersklasse": None, "results": lane_results, "sortierung": 0}]
            if is_staffel_bewerb(bewerb)
            else group_results_by_altersklasse(lane_results, altersklassen)
        )
        results.append(
            {
                "bewerb": bewerb,
                "results": lane_results,
                "altersklassen": result_groups,
            }
        )
    return results


def build_relay_results(db):
    lanes = (
        db.query(LaufBahn)
        .options(
            joinedload(LaufBahn.teilnehmer),
            joinedload(LaufBahn.lauf).joinedload(Lauf.bewerb).joinedload(Bewerb.jahrgang),
        )
        .join(Teilnehmer, LaufBahn.teilnehmer_id == Teilnehmer.id)
        .filter(LaufBahn.zeit_ms > 0)
        .filter(Teilnehmer.staffel != "")
        .all()
    )

    relays = {}
    for lane in lanes:
        if not is_staffel_bewerb(lane.lauf.bewerb):
            continue
        relay_name = lane.teilnehmer.staffel.strip()
        if not relay_name:
            continue
        relay = relays.setdefault(
            relay_name,
            {"staffel": relay_name, "zeit_ms": lane.zeit_ms, "starts": [], "teilnehmer": set()},
        )
        relay["teilnehmer"].add(lane.teilnehmer.display_name())
        relay["starts"].append(
            {
                "staffel": relay_name,
                "bewerb": lane.lauf.bewerb.full_name(),
                "lauf": lane.lauf.laufnummer,
                "bahn": lane.bahn,
                "zeit": format_ms(lane.zeit_ms),
            }
        )

    relay_rows = list(relays.values())
    if not relay_rows:
        return [], 0

    average_ms = round(sum(item["zeit_ms"] for item in relay_rows) / len(relay_rows))
    for item in relay_rows:
        item["abweichung_ms"] = abs(item["zeit_ms"] - average_ms)
        item["teilnehmer"] = sorted(
            participant.display_name()
            for participant in db.query(Teilnehmer)
            .filter(Teilnehmer.staffel == item["staffel"])
            .order_by(Teilnehmer.id)
            .all()
        )
        item["starts"].sort(key=lambda start: (start["bewerb"], start["lauf"], start["bahn"]))

    relay_rows.sort(key=lambda item: (item["abweichung_ms"], item["zeit_ms"], item["staffel"]))
    return relay_rows, average_ms


def build_ortsmeister_results(db, selected_bewerb_ids, gender):
    if not selected_bewerb_ids:
        return []

    selected_bewerbe = (
        db.query(Bewerb)
        .options(joinedload(Bewerb.jahrgang))
        .filter(Bewerb.id.in_(selected_bewerb_ids))
        .order_by(Bewerb.id)
        .all()
    )
    selected_ids = [bewerb.id for bewerb in selected_bewerbe]
    lanes = (
        db.query(LaufBahn)
        .options(
            joinedload(LaufBahn.teilnehmer),
            joinedload(LaufBahn.lauf).joinedload(Lauf.bewerb).joinedload(Bewerb.jahrgang),
        )
        .join(Lauf, LaufBahn.lauf_id == Lauf.id)
        .join(Teilnehmer, LaufBahn.teilnehmer_id == Teilnehmer.id)
        .filter(Lauf.bewerb_id.in_(selected_ids))
        .filter(LaufBahn.zeit_ms > 0)
        .all()
    )

    participants = {}
    for lane in lanes:
        if lane.teilnehmer.gast:
            continue
        if normalized_gender(lane.teilnehmer.geschlecht) != gender:
            continue
        participant = participants.setdefault(
            lane.teilnehmer_id,
            {"teilnehmer": lane.teilnehmer, "zeiten": {}},
        )
        current = participant["zeiten"].get(lane.lauf.bewerb_id)
        if current is None or lane.zeit_ms < current["zeit_ms"]:
            participant["zeiten"][lane.lauf.bewerb_id] = {
                "zeit_ms": lane.zeit_ms,
                "bewerb": lane.lauf.bewerb,
            }

    rows = []
    for participant_data in participants.values():
        if any(bewerb_id not in participant_data["zeiten"] for bewerb_id in selected_ids):
            continue
        total_ms = sum(participant_data["zeiten"][bewerb_id]["zeit_ms"] for bewerb_id in selected_ids)
        rows.append(
            {
                "teilnehmer": participant_data["teilnehmer"],
                "gesamt_ms": total_ms,
                "zeiten": [participant_data["zeiten"][bewerb_id] for bewerb_id in selected_ids],
            }
        )

    rows.sort(key=lambda item: (item["gesamt_ms"], item["teilnehmer"].id))
    return rows


def distance_group(bewerb):
    text_value = f"{bewerb.distanz} {bewerb.name}".lower()
    match = re.search(r"(^|\D)(50|100)\s*m?", text_value)
    if not match:
        return None
    return f"{match.group(2)}m"


def build_day_fastest_results(db):
    lanes = (
        db.query(LaufBahn)
        .options(
            joinedload(LaufBahn.teilnehmer),
            joinedload(LaufBahn.lauf).joinedload(Lauf.bewerb).joinedload(Bewerb.jahrgang),
        )
        .join(Lauf, LaufBahn.lauf_id == Lauf.id)
        .join(Bewerb, Lauf.bewerb_id == Bewerb.id)
        .filter(LaufBahn.zeit_ms > 0)
        .all()
    )

    groups = {
        "50m": {"maennlich": [], "weiblich": []},
        "100m": {"maennlich": [], "weiblich": []},
    }
    for lane in lanes:
        if is_staffel_bewerb(lane.lauf.bewerb):
            continue
        distance = distance_group(lane.lauf.bewerb)
        gender = normalized_gender(lane.teilnehmer.geschlecht) if lane.teilnehmer else ""
        if distance not in groups or gender not in groups[distance]:
            continue
        groups[distance][gender].append(
            {
                "teilnehmer": lane.teilnehmer,
                "bewerb": lane.lauf.bewerb,
                "lauf": lane.lauf.laufnummer,
                "bahn": lane.bahn,
                "zeit_ms": lane.zeit_ms,
            }
        )

    for gender_groups in groups.values():
        for rows in gender_groups.values():
            rows.sort(key=lambda item: (item["zeit_ms"], item["teilnehmer"].id))
    return groups


def results_csv(results):
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Platz", "Bewerb", "Altersklasse", "Teilnehmer", "Zeit"])
    for bewerb_data in results:
        for group in bewerb_data["altersklassen"]:
            for rank, item in enumerate(group["results"], start=1):
                writer.writerow(
                    [
                        rank,
                        bewerb_data["bewerb"].full_name(),
                        result_group_label(bewerb_data["bewerb"], group),
                        item["teilnehmer"].display_name(),
                        format_ms(item["zeit_ms"]),
                    ]
                )
    return output.getvalue().encode("utf-8")


def draw_pdf_frame(canvas, doc):
    width, height = A4
    red = colors.HexColor("#c4001a")
    dark = colors.HexColor("#1f2933")
    light = colors.HexColor("#f3f5f7")

    canvas.saveState()
    canvas.setFillColor(red)
    canvas.rect(0, height - 24 * mm, width, 24 * mm, fill=True, stroke=False)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(18 * mm, height - 14 * mm, "Oesterreichische Wasserrettung")
    canvas.setFont("Helvetica", 9)
    canvas.drawString(18 * mm, height - 19 * mm, "LV Oberoesterreich - Ortsstelle Vorchdorf")
    canvas.setFillColor(colors.white)
    canvas.roundRect(width - 48 * mm, height - 22 * mm, 32 * mm, 18 * mm, 2 * mm, fill=True, stroke=False)
    draw_logo(canvas, 164, 276, 28, 14)

    canvas.setFillColor(light)
    canvas.rect(0, 0, width, 13 * mm, fill=True, stroke=False)
    canvas.setFillColor(dark)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(18 * mm, 6 * mm, "Ortsmeisterschaften Ergebnisliste")
    canvas.drawRightString(width - 18 * mm, 6 * mm, f"Seite {doc.page}")
    canvas.restoreState()


def result_table_style():
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2933")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7dce1")),
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#c4001a")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
    )


def build_results_pdf(results):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=34 * mm,
        bottomMargin=20 * mm,
        title="Ergebnisse Ortsmeisterschaften",
    )

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ResultTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=25,
            textColor=colors.HexColor("#1f2933"),
            alignment=TA_CENTER,
            spaceAfter=8 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ResultMeta",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#4a5560"),
            alignment=TA_CENTER,
            spaceAfter=9 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CompetitionTitle",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#c4001a"),
            spaceBefore=5 * mm,
            spaceAfter=2 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="AgeClassTitle",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=colors.HexColor("#1f2933"),
            spaceBefore=2 * mm,
            spaceAfter=1 * mm,
        )
    )

    story = [
        Paragraph("Ortsmeisterschaften", styles["ResultTitle"]),
        Paragraph(
            f"Ergebnisliste · Stand {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            styles["ResultMeta"],
        ),
    ]

    table_header = ["Platz", "Teilnehmer", "Lauf", "Bahn", "Zeit"]
    for bewerb_data in results:
        story.append(Paragraph(bewerb_data["bewerb"].full_name(), styles["CompetitionTitle"]))
        if not bewerb_data["results"]:
            rows = [table_header, ["-", "Noch keine Zeiten vorhanden", "-", "-", "-"]]
            table = Table(rows, colWidths=[17 * mm, 82 * mm, 18 * mm, 18 * mm, 28 * mm], repeatRows=1)
            table.setStyle(result_table_style())
            story.extend([table, Spacer(1, 4 * mm)])
            continue

        for group in bewerb_data["altersklassen"]:
            story.append(Paragraph(result_group_label(bewerb_data["bewerb"], group), styles["AgeClassTitle"]))
            rows = [table_header]
            for rank, item in enumerate(group["results"], start=1):
                rows.append(
                    [
                        str(rank),
                        item["teilnehmer"].display_name(),
                        str(item["lauf"].laufnummer),
                        str(item["bahn"].bahn),
                        format_ms(item["zeit_ms"]),
                    ]
                )
            table = Table(rows, colWidths=[17 * mm, 82 * mm, 18 * mm, 18 * mm, 28 * mm], repeatRows=1)
            table.setStyle(result_table_style())
            story.extend([table, Spacer(1, 3 * mm)])

    doc.build(story, onFirstPage=draw_pdf_frame, onLaterPages=draw_pdf_frame)
    buffer.seek(0)
    return buffer.getvalue()


def build_startlist_pdf(bewerbe):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=34 * mm,
        bottomMargin=20 * mm,
        title="Startliste Ortsmeisterschaften",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="StartlistTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=25,
            textColor=colors.HexColor("#1f2933"),
            alignment=TA_CENTER,
            spaceAfter=8 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="StartlistMeta",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#4a5560"),
            alignment=TA_CENTER,
            spaceAfter=9 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="StartlistCompetition",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#c4001a"),
            spaceBefore=5 * mm,
            spaceAfter=2 * mm,
        )
    )
    story = [
        Paragraph("Startliste", styles["StartlistTitle"]),
        Paragraph(f"Stand {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles["StartlistMeta"]),
    ]
    run_numbers = global_run_numbers(bewerbe)

    for bewerb in bewerbe:
        story.append(Paragraph(f"Bewerb ID {bewerb.id}: {bewerb.full_name()}", styles["StartlistCompetition"]))
        if not bewerb.laufe:
            story.append(Paragraph("Noch keine Laeufe erzeugt.", styles["Normal"]))
            continue
        is_relay = is_staffel_bewerb(bewerb)
        rows = [["Lauf", "Bahn", "Staffel" if is_relay else "Teilnehmer"]]
        for lauf in sorted(bewerb.laufe, key=lambda item: item.laufnummer):
            for bahn in sorted(lauf.laufbahnen, key=lambda item: item.bahn):
                name = "-"
                if bahn.teilnehmer:
                    name = bahn.teilnehmer.staffel if is_relay else bahn.teilnehmer.display_name()
                rows.append([str(run_numbers[lauf.id]), str(bahn.bahn), name or "-"])

        table = Table(rows, colWidths=[18 * mm, 18 * mm, 125 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2933")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (0, 0), (1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7dce1")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([table, Spacer(1, 4 * mm)])

    doc.build(story, onFirstPage=draw_pdf_frame, onLaterPages=draw_pdf_frame)
    buffer.seek(0)
    return buffer.getvalue()


def build_full_report_pdf(db):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=34 * mm,
        bottomMargin=20 * mm,
        title="Bericht Ortsmeisterschaften",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=25,
            textColor=colors.HexColor("#1f2933"),
            alignment=TA_CENTER,
            spaceAfter=8 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportSection",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor("#c4001a"),
            spaceBefore=5 * mm,
            spaceAfter=2 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportSubsection",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=colors.HexColor("#1f2933"),
            spaceBefore=2 * mm,
            spaceAfter=1 * mm,
        )
    )

    def add_table(story, rows, widths):
        table = Table(rows, colWidths=widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2933")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7dce1")),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend([table, Spacer(1, 4 * mm)])

    participants = db.query(Teilnehmer).order_by(Teilnehmer.id).all()
    bewerbe = db.query(Bewerb).options(joinedload(Bewerb.jahrgang)).order_by(Bewerb.id).all()
    results = build_results(db)
    relay_rows, relay_average = build_relay_results(db)
    day_fastest = build_day_fastest_results(db)

    story = [
        Paragraph("Bericht Ortsmeisterschaften", styles["ReportTitle"]),
        Paragraph(f"Stand {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles["Normal"]),
        Spacer(1, 5 * mm),
    ]

    relay_count = len({p.staffel.strip() for p in participants if p.staffel and p.staffel.strip()})
    story.append(Paragraph("Kennzahlen", styles["ReportSection"]))
    add_table(
        story,
        [
            ["Kennzahl", "Wert"],
            ["Teilnehmer gesamt", str(len(participants))],
            ["Teilnehmer Freistil", str(sum(1 for p in participants if p.freistil))],
            ["Teilnehmer Brust", str(sum(1 for p in participants if p.brust))],
            ["Gaeste", str(sum(1 for p in participants if p.gast))],
            ["Staffeln", str(relay_count)],
            ["Bewerbe", str(len(bewerbe))],
        ],
        [80 * mm, 35 * mm],
    )

    story.append(Paragraph("Teilnehmer", styles["ReportSection"]))
    participant_rows_pdf = [["ID", "Name", "Jg.", "G", "Brust", "Freistil", "Gast", "Staffel"]]
    for p in participants:
        participant_rows_pdf.append(
            [
                str(p.id),
                p.display_name(),
                str(p.geburtsjahr),
                p.geschlecht,
                "ja" if p.brust else "nein",
                "ja" if p.freistil else "nein",
                "ja" if p.gast else "nein",
                p.staffel or "-",
            ]
        )
    add_table(story, participant_rows_pdf, [10 * mm, 42 * mm, 14 * mm, 10 * mm, 18 * mm, 20 * mm, 14 * mm, 30 * mm])

    story.append(Paragraph("Bewerbe", styles["ReportSection"]))
    competition_rows = [["ID", "Bewerb", "OM"]]
    for b in bewerbe:
        competition_rows.append([str(b.id), b.full_name(), "ja" if b.ortsmeister_relevant else "nein"])
    add_table(story, competition_rows, [12 * mm, 130 * mm, 16 * mm])

    story.append(Paragraph("Startlisten", styles["ReportSection"]))
    run_numbers = global_run_numbers(bewerbe)
    for b in bewerbe:
        is_relay = is_staffel_bewerb(b)
        story.append(Paragraph(f"Bewerb ID {b.id}: {b.full_name()}", styles["ReportSubsection"]))
        if not b.laufe:
            story.append(Paragraph("Noch keine Laeufe erzeugt.", styles["Normal"]))
            story.append(Spacer(1, 2 * mm))
            continue
        for lauf in sorted(b.laufe, key=lambda item: item.laufnummer):
            story.append(Paragraph(f"Lauf {run_numbers[lauf.id]}", styles["Normal"]))
            start_rows = [["Lauf", "Bahn", "Teilnehmer/Staffel"]]
            for bahn in sorted(lauf.laufbahnen, key=lambda item: item.bahn):
                name = "-"
                if bahn.teilnehmer:
                    name = bahn.teilnehmer.staffel if is_relay else bahn.teilnehmer.display_name()
                start_rows.append([str(run_numbers[lauf.id]), str(bahn.bahn), name or "-"])
            add_table(story, start_rows, [18 * mm, 18 * mm, 122 * mm])

    story.append(Paragraph("Ergebnisse", styles["ReportSection"]))
    for bewerb_data in results:
        is_relay = is_staffel_bewerb(bewerb_data["bewerb"])
        result_groups = [{"altersklasse": None, "results": bewerb_data["results"]}] if is_relay else bewerb_data["altersklassen"]
        story.append(
            Paragraph(
                f"Bewerb ID {bewerb_data['bewerb'].id}: {bewerb_data['bewerb'].full_name()}",
                styles["ReportSubsection"],
            )
        )
        if not bewerb_data["results"]:
            story.append(Paragraph("Noch keine Zeiten vorhanden.", styles["Normal"]))
            story.append(Spacer(1, 2 * mm))
            continue
        for group in result_groups:
            story.append(Paragraph(result_group_label(bewerb_data["bewerb"], group), styles["Normal"]))
            result_rows = [["Platz", "Teilnehmer/Staffel", "Lauf", "Bahn", "Zeit"]]
            for rank, item in enumerate(group["results"], start=1):
                name = item["teilnehmer"].staffel if is_relay and item["teilnehmer"].staffel else item["teilnehmer"].display_name()
                result_rows.append(
                    [
                        str(rank),
                        name,
                        str(item["lauf"].laufnummer),
                        str(item["bahn"].bahn),
                        format_ms(item["zeit_ms"]),
                    ]
                )
            add_table(story, result_rows, [14 * mm, 82 * mm, 18 * mm, 18 * mm, 26 * mm])

    story.append(Paragraph("Ortsmeister", styles["ReportSection"]))
    om_bewerbe_m = [b for b in bewerbe if not is_staffel_bewerb(b) and b.ortsmeister_relevant and normalized_gender(b.geschlecht) in {"maennlich", "mixed"}]
    om_bewerbe_w = [b for b in bewerbe if not is_staffel_bewerb(b) and b.ortsmeister_relevant and normalized_gender(b.geschlecht) in {"weiblich", "mixed"}]
    om_rows = [["Kategorie", "Teilnehmer", "Gesamtzeit"]]
    for label, gender, selected in [("Maennlich", "maennlich", om_bewerbe_m), ("Weiblich", "weiblich", om_bewerbe_w)]:
        rows = build_ortsmeister_results(db, [b.id for b in selected], gender)
        if rows:
            winner = rows[0]
            om_rows.append([label, winner["teilnehmer"].display_name(), format_ms(winner["gesamt_ms"])])
    add_table(story, om_rows, [34 * mm, 88 * mm, 36 * mm])

    story.append(Paragraph("Tagesschnellste", styles["ReportSection"]))
    fastest_rows = [["Distanz", "Kategorie", "Teilnehmer", "Zeit"]]
    for distance, gender_groups in day_fastest.items():
        for gender, rows in gender_groups.items():
            label = "Maennlich" if gender == "maennlich" else "Weiblich"
            if rows:
                winner = rows[0]
                fastest_rows.append([distance, label, winner["teilnehmer"].display_name(), format_ms(winner["zeit_ms"])])
    add_table(story, fastest_rows, [24 * mm, 32 * mm, 76 * mm, 26 * mm])

    story.append(Paragraph("Staffelwertung", styles["ReportSection"]))
    relay_table_rows = [["Rang", "Staffel", "Zeit", "Abweichung"]]
    for rank, item in enumerate(relay_rows, start=1):
        relay_table_rows.append([str(rank), item["staffel"], format_ms(item["zeit_ms"]), format_ms(item["abweichung_ms"])])
    if relay_average:
        relay_table_rows.append(["", "Durchschnitt", format_ms(relay_average), ""])
    add_table(story, relay_table_rows, [14 * mm, 86 * mm, 28 * mm, 30 * mm])

    doc.build(story, onFirstPage=draw_pdf_frame, onLaterPages=draw_pdf_frame)
    buffer.seek(0)
    return buffer.getvalue()


def certificate_rows(results, max_place):
    rows = []
    for bewerb_data in results:
        is_relay = is_staffel_bewerb(bewerb_data["bewerb"])
        result_groups = [{"altersklasse": None, "results": bewerb_data["results"]}] if is_relay else bewerb_data["altersklassen"]
        for group in result_groups:
            for place, item in enumerate(group["results"], start=1):
                if max_place and place > max_place:
                    continue
                certificate_name = (
                    item["teilnehmer"].staffel
                    if is_relay and item["teilnehmer"] and item["teilnehmer"].staffel
                    else item["teilnehmer"].display_name()
                )
                competition = bewerb_data["bewerb"].full_name()
                age_class = result_group_label(bewerb_data["bewerb"], group)
                copies = 4 if is_relay else 1
                for copy_index in range(1, copies + 1):
                    rows.append(
                        {
                            "place": place,
                            "name": certificate_name,
                            "competition": competition,
                            "age_class": age_class,
                            "time": format_ms(item["zeit_ms"]),
                            "copy": copy_index,
                        }
                    )
    return rows


def ortsmeister_certificate_rows(db):
    bewerbe = db.query(Bewerb).options(joinedload(Bewerb.jahrgang)).order_by(Bewerb.id).all()
    male_bewerbe = [
        bewerb
        for bewerb in bewerbe
        if not is_staffel_bewerb(bewerb)
        and bool(bewerb.ortsmeister_relevant)
        and normalized_gender(bewerb.geschlecht) in {"maennlich", "mixed"}
    ]
    female_bewerbe = [
        bewerb
        for bewerb in bewerbe
        if not is_staffel_bewerb(bewerb)
        and bool(bewerb.ortsmeister_relevant)
        and normalized_gender(bewerb.geschlecht) in {"weiblich", "mixed"}
    ]
    rows = []
    for label, gender, selected_bewerbe in [
        ("Ortsmeister Maennlich", "maennlich", male_bewerbe),
        ("Ortsmeister Weiblich", "weiblich", female_bewerbe),
    ]:
        results = build_ortsmeister_results(db, [bewerb.id for bewerb in selected_bewerbe], gender)
        for place, item in enumerate(results, start=1):
            rows.append(
                {
                    "place": place,
                    "name": item["teilnehmer"].display_name(),
                    "competition": label,
                    "age_class": "",
                    "time": format_ms(item["gesamt_ms"]),
                    "copy": 1,
                }
            )
    return rows


def relay_certificate_rows(db):
    relay_rows, _average_ms = build_relay_results(db)
    rows = []
    for place, item in enumerate(relay_rows, start=1):
        for copy_index in range(1, 5):
            rows.append(
                {
                    "place": place,
                    "name": item["staffel"],
                    "competition": "Staffelwertung",
                    "age_class": "Staffel",
                    "time": format_ms(item["zeit_ms"]),
                    "copy": copy_index,
                }
            )
    return rows


def draw_centered_certificate_text(canvas, text, x_mm, y_mm, font_name, font_size):
    width, height = A4
    canvas.setFont(font_name, font_size)
    canvas.drawCentredString(x_mm * mm, height - y_mm * mm, text)


def build_certificates_pdf(rows, settings):
    buffer = BytesIO()
    pdf = pdf_canvas.Canvas(buffer, pagesize=A4)
    date_text = settings["date_text"].strip()

    for row in rows:
        if settings.get("name_visible", True):
            draw_centered_certificate_text(
                pdf,
                row["name"],
                settings["name_x"],
                settings["name_y"],
                "Helvetica-Bold",
                settings["name_size"],
            )
        if settings.get("competition_visible", True):
            draw_centered_certificate_text(
                pdf,
                row["competition"],
                settings["competition_x"],
                settings["competition_y"],
                "Helvetica",
                settings["competition_size"],
            )
        if settings.get("age_class_visible", True) and row.get("age_class"):
            draw_centered_certificate_text(
                pdf,
                row["age_class"],
                settings["age_class_x"],
                settings["age_class_y"],
                "Helvetica",
                settings["age_class_size"],
            )
        if settings.get("place_visible", True):
            draw_centered_certificate_text(
                pdf,
                str(row["place"]),
                settings["place_x"],
                settings["place_y"],
                "Helvetica-Bold",
                settings["place_size"],
            )
        if settings.get("time_visible", True):
            draw_centered_certificate_text(
                pdf,
                row["time"],
                settings["time_x"],
                settings["time_y"],
                "Helvetica",
                settings["time_size"],
            )
        if settings.get("date_visible", True) and date_text:
            draw_centered_certificate_text(
                pdf,
                date_text,
                settings["date_x"],
                settings["date_y"],
                "Helvetica",
                settings["date_size"],
            )
        pdf.showPage()

    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def render_certificate_pdf_preview(row, settings):
    if not row:
        row = {
            "place": 1,
            "name": "Max Mustermann",
            "competition": "50m Brust maennlich (2010-1949)",
            "age_class": "Jugend - 50 m (Jg. 2015)",
            "time": "01:15.20",
        }
    preview_pdf = build_certificates_pdf([row], settings)
    document = fitz.open(stream=preview_pdf, filetype="pdf")
    page = document.load_page(0)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
    encoded_png = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
    st.markdown(
        f"""
        <div style="
            aspect-ratio: 1 / 1.414;
            width: min(100%, 105mm);
            margin: 0 auto;
            overflow: auto;
            border: 1px solid #d7dce1;
            box-shadow: 0 8px 22px rgba(31, 41, 51, 0.12);
            background: #f7f8fa;
        ">
            <img src="data:image/png;base64,{encoded_png}" style="width: 100%; display: block;" />
        </div>
        """,
        unsafe_allow_html=True,
    )


def page_home(db):
    st.title("Ortsmeisterschaften")
    st.caption("Verwaltung fuer Anmeldung, Startlisten, Zeitnehmung und Ergebnisse.")

    participants = db.query(Teilnehmer).all()
    total_participants = len(participants)
    freestyle_count = sum(1 for participant in participants if participant.freistil)
    breaststroke_count = sum(1 for participant in participants if participant.brust)
    relay_count = len(
        {
            participant.staffel.strip()
            for participant in participants
            if participant.staffel and participant.staffel.strip()
        }
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Gesamtanzahl Teilnehmer", total_participants)
    col2.metric("Teilnehmer Freistil", freestyle_count)
    col3.metric("Teilnehmer Brust", breaststroke_count)
    col4.metric("Staffeln", relay_count)

    st.subheader("Naechste Schritte")
    st.write("Jahrgaenge und Bewerbe anlegen, Teilnehmende erfassen, Startlisten erzeugen und Zeiten eintragen.")

    st.subheader("Bericht und Neustart")
    action_col1, action_col2 = st.columns(2)
    with action_col1:
        st.download_button(
            "Vollstaendigen PDF Bericht herunterladen",
            data=build_full_report_pdf(db),
            file_name="bericht-ortsmeisterschaft.pdf",
            mime="application/pdf",
            type="primary",
        )

    with action_col2:
        backup_data = export_backup(db)
        st.download_button(
            "Zwischenspeicher vor Neustart herunterladen",
            data=backup_data,
            file_name="ortsmeisterschaft-zwischenspeicher-vor-neustart.json",
            mime="application/json",
        )
        reset_form_version = st.session_state.setdefault("reset_form_version", 0)
        backup_confirmed = st.checkbox(
            "Zwischenspeicher wurde heruntergeladen",
            key=f"reset_backup_confirmed_{reset_form_version}",
        )
        reset_confirmed = st.checkbox(
            "Ich moechte alle Meisterschaftsdaten loeschen",
            key=f"reset_confirmed_{reset_form_version}",
        )
        if st.button(
            "Neue Meisterschaft beginnen",
            disabled=not (backup_confirmed and reset_confirmed),
        ):
            reset_championship(db)
            clear_reset_checkboxes()
            st.success("Neue Meisterschaft wurde gestartet. Alle eingegebenen Daten wurden zurueckgesetzt.")
            refresh()


def page_anmeldung(db):
    st.title("Anmeldung")

    form_col, import_col = st.columns([1, 1])
    with form_col:
        st.subheader("Teilnehmer erfassen")
        with st.form("participant_form", clear_on_submit=True):
            vorname = st.text_input("Vorname")
            nachname = st.text_input("Nachname")
            year_col, gender_col = st.columns(2)
            geburtsjahr = year_col.number_input(
                "Geburtsjahr", min_value=1900, max_value=2100, value=2012, step=1
            )
            geschlecht = gender_col.selectbox("Geschlecht", ["m", "w"])
            discipline_col, relay_col = st.columns([1, 1])
            brust = discipline_col.checkbox("Brust")
            freistil = discipline_col.checkbox("Freistil")
            gast = discipline_col.checkbox("Gast")
            staffel = relay_col.text_input("Staffel")
            submitted = st.form_submit_button("Speichern", type="primary")

        if submitted:
            if vorname.strip() and nachname.strip() and geschlecht:
                participant = Teilnehmer(
                    vorname=vorname.strip(),
                    nachname=nachname.strip(),
                    geburtsjahr=int(geburtsjahr),
                    geschlecht=geschlecht,
                    brust=brust,
                    freistil=freistil,
                    gast=gast,
                    staffel=staffel.strip(),
                )
                db.add(participant)
                db.flush()
                assign_bewerbe_for_teilnehmer(participant, db)
                st.success("Teilnehmer erfolgreich gespeichert.")
                refresh()
            else:
                st.error("Bitte alle Pflichtfelder korrekt ausfuellen.")

    with import_col:
        st.subheader("CSV Import")
        st.caption("Format: Vorname;Nachname;Geburtsjahr;Geschlecht;Brust;Freistil;Staffel;Gast")
        template = "Vorname;Nachname;Geburtsjahr;Geschlecht;Brust;Freistil;Staffel;Gast\r\nMax;Mustermann;2012;m;ja;nein;Team A;nein\r\nErika;Musterfrau;2011;w;nein;ja;Team A;ja\r\n"
        st.download_button(
            "CSV Vorlage herunterladen",
            data=template.encode("utf-8"),
            file_name="anmeldung-vorlage.csv",
            mime="text/csv",
        )
        uploaded_file = st.file_uploader("CSV-Datei", type=["csv", "txt"])
        if st.button("Import starten", disabled=uploaded_file is None):
            text = uploaded_file.getvalue().decode("utf-8", errors="replace")
            rows = csv.reader(StringIO(text), delimiter=";")
            next(rows, None)
            imported = 0
            for row in rows:
                if len(row) < 7:
                    continue
                values = [item.strip() for item in row]
                vorname, nachname, geburtsjahr, geschlecht, brust, freistil, staffel = values[:7]
                gast = values[7] if len(values) > 7 else ""
                if not geburtsjahr.isdigit() or not vorname or not nachname:
                    continue
                participant = Teilnehmer(
                    vorname=vorname,
                    nachname=nachname,
                    geburtsjahr=int(geburtsjahr),
                    geschlecht=geschlecht,
                    brust=parse_bool(brust),
                    freistil=parse_bool(freistil),
                    gast=parse_bool(gast),
                    staffel=staffel,
                )
                db.add(participant)
                db.flush()
                assign_bewerbe_for_teilnehmer(participant, db)
                imported += 1
            st.success(f"{imported} Teilnehmende aus CSV importiert.")
            refresh()

    st.subheader("Teilnehmer")
    participants = (
        db.query(Teilnehmer)
        .options(
            joinedload(Teilnehmer.anmeldungen)
            .joinedload(Anmeldung.bewerb)
            .joinedload(Bewerb.jahrgang)
        )
        .order_by(Teilnehmer.id)
        .all()
    )

    if participants:
        participant_options = {
            f"{participant.display_name()} ({participant.geburtsjahr}, ID {participant.id})": participant
            for participant in participants
        }
        with st.expander("Teilnehmer verwalten", expanded=False):
            if st.button("Alle Zuordnungen aktualisieren"):
                update_assignments_for_all_participants(db)
                st.success("Alle Zuordnungen wurden aktualisiert.")
                refresh()

            selected_name = st.selectbox("Teilnehmer", list(participant_options.keys()))
            selected_participant = participant_options[selected_name]

            with st.form(f"edit_participant_{selected_participant.id}"):
                edit_col1, edit_col2 = st.columns(2)
                new_vorname = edit_col1.text_input("Vorname", value=selected_participant.vorname)
                new_nachname = edit_col2.text_input("Nachname", value=selected_participant.nachname)
                meta_col1, meta_col2, meta_col3 = st.columns(3)
                new_geburtsjahr = meta_col1.number_input(
                    "Geburtsjahr",
                    min_value=1900,
                    max_value=2100,
                    value=selected_participant.geburtsjahr,
                    step=1,
                )
                gender_options = ["m", "w"]
                gender_index = gender_options.index(selected_participant.geschlecht) if selected_participant.geschlecht in gender_options else 0
                new_geschlecht = meta_col2.selectbox("Geschlecht", gender_options, index=gender_index)
                new_staffel = meta_col3.text_input("Staffel", value=selected_participant.staffel or "")
                disc_col1, disc_col2 = st.columns(2)
                new_brust = disc_col1.checkbox("Brust", value=bool(selected_participant.brust))
                new_freistil = disc_col2.checkbox("Freistil", value=bool(selected_participant.freistil))
                new_gast = disc_col2.checkbox("Gast", value=bool(selected_participant.gast))
                save_participant = st.form_submit_button("Teilnehmer speichern und Zuordnung aktualisieren", type="primary")

            action_col1, action_col2 = st.columns(2)
            refresh_assignment = action_col1.button(
                "Zuordnung aktualisieren",
                key=f"refresh_assignment_{selected_participant.id}",
            )
            delete_selected = action_col2.button(
                "Teilnehmer loeschen",
                key=f"delete_participant_{selected_participant.id}",
            )

            if save_participant:
                if new_vorname.strip() and new_nachname.strip():
                    selected_participant.vorname = new_vorname.strip()
                    selected_participant.nachname = new_nachname.strip()
                    selected_participant.geburtsjahr = int(new_geburtsjahr)
                    selected_participant.geschlecht = new_geschlecht
                    selected_participant.staffel = new_staffel.strip()
                    selected_participant.brust = new_brust
                    selected_participant.freistil = new_freistil
                    selected_participant.gast = new_gast
                    update_assignments_for_participant(selected_participant, db)
                    st.success("Teilnehmer und Zuordnung wurden aktualisiert.")
                    refresh()
                else:
                    st.error("Vorname und Nachname duerfen nicht leer sein.")

            if refresh_assignment:
                update_assignments_for_participant(selected_participant, db)
                st.success("Zuordnung wurde aktualisiert.")
                refresh()

            if delete_selected:
                delete_participant(selected_participant, db)
                st.success("Teilnehmer wurde geloescht.")
                refresh()

    rows = participant_rows(participants)
    if rows:
        edited_participants = st.data_editor(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            disabled=["ID", "Name", "Zugeordnete Bewerbe"],
            column_config={
                "Geschlecht": st.column_config.SelectboxColumn("Geschlecht", options=["m", "w"]),
                "Geburtsjahr": st.column_config.NumberColumn("Geburtsjahr", min_value=1900, max_value=2100, step=1),
                "Brust": st.column_config.CheckboxColumn("Brust"),
                "Freistil": st.column_config.CheckboxColumn("Freistil"),
                "Gast": st.column_config.CheckboxColumn("Gast"),
                "Loeschen": st.column_config.CheckboxColumn("Loeschen"),
            },
            key="participants_editor",
        )
        if st.button("Tabellen-Aenderungen speichern", type="primary"):
            for row in edited_participants.to_dict("records"):
                participant = db.query(Teilnehmer).get(int(row["ID"]))
                if not participant:
                    continue
                if row.get("Loeschen"):
                    delete_participant(participant, db)
                    continue
                if str(row["Vorname"]).strip() and str(row["Nachname"]).strip():
                    participant.vorname = str(row["Vorname"]).strip()
                    participant.nachname = str(row["Nachname"]).strip()
                    participant.geburtsjahr = int(row["Geburtsjahr"])
                    participant.geschlecht = str(row["Geschlecht"]).strip()
                    participant.brust = bool(row["Brust"])
                    participant.freistil = bool(row["Freistil"])
                    participant.gast = bool(row["Gast"])
                    staffel_value = "" if str(row["Staffel"]).strip() == "-" else str(row["Staffel"]).strip()
                    participant.staffel = staffel_value
                    update_assignments_for_participant(participant, db)
            st.success("Teilnehmer-Tabelle wurde gespeichert.")
            refresh()
    else:
        st.info("Keine Teilnehmer vorhanden.")


def page_settings(db):
    st.title("Einstellungen")
    jahrgang_col, bewerb_col = st.columns([1, 1])

    with jahrgang_col:
        st.subheader("Jahrgang anlegen")
        with st.form("jahrgang_form", clear_on_submit=True):
            name = st.text_input("Name", placeholder="2011-2015")
            jahr_von = st.number_input("Jahr von", min_value=0, max_value=2100, value=2011)
            jahr_bis = st.number_input("Jahr bis", min_value=0, max_value=2100, value=2015)
            submitted = st.form_submit_button("Jahrgang speichern", type="primary")

        if submitted:
            if name.strip() and int(jahr_von) <= int(jahr_bis):
                db.add(Jahrgang(name=name.strip(), jahr_von=int(jahr_von), jahr_bis=int(jahr_bis)))
                db.commit()
                st.success("Jahrgang erfolgreich angelegt.")
                refresh()
            else:
                st.error("Bitte gueltige Angaben fuer den Jahrgang eintragen.")

    with bewerb_col:
        st.subheader("Bewerb anlegen")
        jahrgaenge = db.query(Jahrgang).order_by(Jahrgang.id).all()
        jahrgang_options = {jahrgang.name: jahrgang.id for jahrgang in jahrgaenge}
        with st.form("bewerb_form", clear_on_submit=True):
            name = st.text_input("Name", placeholder="50m Brust weiblich")
            distance_col, style_col = st.columns(2)
            distanz = distance_col.text_input("Distanz", placeholder="50m")
            stil = style_col.selectbox("Stil", ["Brust", "Freistil", "Staffel"])
            geschlecht = st.selectbox("Geschlecht", ["weiblich", "maennlich", "mixed"])
            jahrgang_name = None
            if stil != "Staffel":
                if jahrgang_options:
                    jahrgang_name = st.selectbox("Jahrgang", list(jahrgang_options.keys()))
                else:
                    st.info("Lege zuerst einen Jahrgang an oder waehle Stil Staffel.")
            submitted = st.form_submit_button("Bewerb speichern", type="primary")

        if submitted:
            if name.strip() and distanz.strip() and stil and geschlecht and (stil == "Staffel" or jahrgang_name):
                jahrgang_id = (
                    get_or_create_relay_jahrgang(db).id
                    if stil == "Staffel"
                    else jahrgang_options[jahrgang_name]
                )
                db.add(
                    Bewerb(
                        name=name.strip(),
                        stil=stil,
                        geschlecht=geschlecht,
                        distanz=distanz.strip(),
                        ortsmeister_relevant=False,
                        ortsmeister_maennlich=False,
                        ortsmeister_weiblich=False,
                        jahrgang_id=jahrgang_id,
                    )
                )
                db.commit()
                for participant in db.query(Teilnehmer).all():
                    assign_bewerbe_for_teilnehmer(participant, db)
                st.success("Bewerb erfolgreich angelegt.")
                refresh()
            else:
                st.error("Bitte alle Bewerbsdaten ausfuellen.")

    st.subheader("Jahrgaenge")
    jahrgaenge = db.query(Jahrgang).order_by(Jahrgang.id).all()
    if jahrgaenge:
        header_cols = st.columns([1, 4, 2, 2, 1])
        header_cols[0].markdown("**ID**")
        header_cols[1].markdown("**Name**")
        header_cols[2].markdown("**Von**")
        header_cols[3].markdown("**Bis**")
        header_cols[4].markdown("**Loeschen**")
        for item in jahrgaenge:
            row_cols = st.columns([1, 4, 2, 2, 1])
            row_cols[0].write(item.id)
            row_cols[1].text_input(
                "Name",
                value=item.name,
                key=f"jahrgang_name_{item.id}",
                label_visibility="collapsed",
            )
            row_cols[2].number_input(
                "Von",
                min_value=0,
                max_value=2100,
                value=item.jahr_von,
                step=1,
                key=f"jahrgang_von_{item.id}",
                label_visibility="collapsed",
            )
            row_cols[3].number_input(
                "Bis",
                min_value=0,
                max_value=2100,
                value=item.jahr_bis,
                step=1,
                key=f"jahrgang_bis_{item.id}",
                label_visibility="collapsed",
            )
            if row_cols[4].button("🗑", key=f"delete_jahrgang_{item.id}", help="Jahrgang loeschen"):
                db.delete(item)
                db.commit()
                update_assignments_for_all_participants(db)
                st.success("Jahrgang wurde geloescht.")
                refresh()

        if st.button("Jahrgaenge speichern", type="primary"):
            for item in jahrgaenge:
                jahrgang = db.query(Jahrgang).get(item.id)
                if not jahrgang:
                    continue
                name = st.session_state.get(f"jahrgang_name_{item.id}", "").strip()
                jahr_von = int(st.session_state.get(f"jahrgang_von_{item.id}", item.jahr_von))
                jahr_bis = int(st.session_state.get(f"jahrgang_bis_{item.id}", item.jahr_bis))
                if name and jahr_von <= jahr_bis:
                    jahrgang.name = name
                    jahrgang.jahr_von = jahr_von
                    jahrgang.jahr_bis = jahr_bis
            db.commit()
            update_assignments_for_all_participants(db)
            st.success("Jahrgaenge wurden gespeichert.")
            st.warning("Bitte Startlisten aktualisieren, falls Jahrgaenge geaendert wurden.")
            refresh()
    else:
        st.info("Keine Jahrgaenge vorhanden.")

    st.subheader("Bewerbe")
    bewerbe = db.query(Bewerb).options(joinedload(Bewerb.jahrgang)).order_by(Bewerb.id).all()
    if bewerbe:
        jahrgang_labels = {
            f"{jahrgang.id} - {jahrgang.name}": jahrgang.id
            for jahrgang in db.query(Jahrgang).order_by(Jahrgang.id).all()
        }
        jahrgang_labels_by_id = {value: label for label, value in jahrgang_labels.items()}
        style_options = ["Brust", "Freistil", "Staffel"]
        gender_options = ["weiblich", "maennlich", "mixed"]
        jahrgang_options = list(jahrgang_labels.keys())
        header_cols = st.columns([1, 4, 2, 2, 2, 2, 3, 1])
        header_cols[0].markdown("**ID**")
        header_cols[1].markdown("**Name**")
        header_cols[2].markdown("**Distanz**")
        header_cols[3].markdown("**Stil**")
        header_cols[4].markdown("**Geschlecht**")
        header_cols[5].markdown("**Ortsmeister**")
        header_cols[6].markdown("**Jahrgang**")
        header_cols[7].markdown("**Loeschen**")
        for item in bewerbe:
            st.session_state.setdefault(
                f"bewerb_ortsmeister_{item.id}",
                bool(item.ortsmeister_maennlich or item.ortsmeister_relevant),
            )
            row_cols = st.columns([1, 4, 2, 2, 2, 2, 3, 1])
            row_cols[0].write(item.id)
            row_cols[1].text_input(
                "Name",
                value=item.name,
                key=f"bewerb_name_{item.id}",
                label_visibility="collapsed",
            )
            row_cols[2].text_input(
                "Distanz",
                value=item.distanz,
                key=f"bewerb_distanz_{item.id}",
                label_visibility="collapsed",
            )
            row_cols[3].selectbox(
                "Stil",
                style_options,
                index=style_options.index(item.stil) if item.stil in style_options else 0,
                key=f"bewerb_stil_{item.id}",
                label_visibility="collapsed",
            )
            row_cols[4].selectbox(
                "Geschlecht",
                gender_options,
                index=gender_options.index(item.geschlecht) if item.geschlecht in gender_options else 0,
                key=f"bewerb_geschlecht_{item.id}",
                label_visibility="collapsed",
            )
            row_cols[5].checkbox(
                "Ortsmeister",
                key=f"bewerb_ortsmeister_{item.id}",
                on_change=save_ortsmeister_flag,
                args=(item.id, f"bewerb_ortsmeister_{item.id}"),
                label_visibility="collapsed",
            )
            row_cols[6].selectbox(
                "Jahrgang",
                jahrgang_options,
                index=jahrgang_options.index(jahrgang_labels_by_id[item.jahrgang_id])
                if item.jahrgang_id in jahrgang_labels_by_id
                else 0,
                key=f"bewerb_jahrgang_{item.id}",
                label_visibility="collapsed",
            )
            if row_cols[7].button("🗑", key=f"delete_bewerb_{item.id}", help="Bewerb loeschen"):
                db.delete(item)
                db.commit()
                update_assignments_for_all_participants(db)
                st.success("Bewerb wurde geloescht.")
                refresh()

        if st.button("Bewerbe speichern", type="primary"):
            for item in bewerbe:
                bewerb = db.query(Bewerb).get(item.id)
                if not bewerb:
                    continue
                name = st.session_state.get(f"bewerb_name_{item.id}", "").strip()
                distanz = st.session_state.get(f"bewerb_distanz_{item.id}", "").strip()
                stil = st.session_state.get(f"bewerb_stil_{item.id}", "").strip()
                geschlecht = st.session_state.get(f"bewerb_geschlecht_{item.id}", "").strip()
                ortsmeister_relevant = bool(st.session_state.get(f"bewerb_ortsmeister_{item.id}", False))
                jahrgang_id = jahrgang_labels.get(st.session_state.get(f"bewerb_jahrgang_{item.id}", ""))
                bewerb.ortsmeister_relevant = ortsmeister_relevant
                bewerb.ortsmeister_maennlich = ortsmeister_relevant
                bewerb.ortsmeister_weiblich = ortsmeister_relevant
                if name and distanz and stil and geschlecht and jahrgang_id:
                    bewerb.name = name
                    bewerb.distanz = distanz
                    bewerb.stil = stil
                    bewerb.geschlecht = geschlecht
                    bewerb.jahrgang_id = int(jahrgang_id)
            db.commit()
            update_assignments_for_all_participants(db)
            st.success("Bewerbe wurden gespeichert.")
            st.warning("Bitte Startlisten aktualisieren, falls Bewerbe geaendert wurden.")
            refresh()
    else:
        st.info("Keine Bewerbe vorhanden.")


def page_altersklassen(db):
    st.title("Altersklassen")
    st.caption("Klasseneinteilung fuer die Auswertung nach Geburtsjahr.")

    with st.form("altersklasse_form", clear_on_submit=True):
        st.subheader("Altersklasse anlegen")
        name_col, dist_col, from_col, to_col, sort_col = st.columns([3, 2, 2, 2, 1])
        name = name_col.text_input("Name", placeholder="Kinder")
        distanz = dist_col.number_input("Distanz in m", min_value=0, max_value=9999, value=25, step=1)
        jahr_von = from_col.number_input("Jahr von", min_value=0, max_value=2100, value=2018, step=1)
        jahr_bis = to_col.number_input("Jahr bis", min_value=0, max_value=2100, value=2100, step=1)
        sortierung = sort_col.number_input("Sort.", min_value=0, max_value=999, value=1, step=1)
        submitted = st.form_submit_button("Altersklasse speichern", type="primary")

    if submitted:
        if name.strip() and int(jahr_von) <= int(jahr_bis):
            db.add(
                Altersklasse(
                    name=name.strip(),
                    distanz=str(int(distanz)) if int(distanz) > 0 else "",
                    jahr_von=int(jahr_von),
                    jahr_bis=int(jahr_bis),
                    sortierung=int(sortierung),
                )
            )
            db.commit()
            st.success("Altersklasse wurde angelegt.")
            refresh()
        else:
            st.error("Bitte gueltige Angaben fuer die Altersklasse eintragen.")

    st.subheader("Klasseneinteilung")
    altersklassen = db.query(Altersklasse).order_by(Altersklasse.sortierung, Altersklasse.id).all()
    if not altersklassen:
        st.info("Keine Altersklassen vorhanden.")
        if st.button("Standard-Klasseneinteilung anlegen", type="primary"):
            create_default_altersklassen()
            st.success("Standard-Klasseneinteilung wurde angelegt.")
            refresh()
        return

    header_cols = st.columns([1, 3, 2, 2, 2, 3, 1])
    header_cols[0].markdown("**Sort.**")
    header_cols[1].markdown("**Klasse**")
    header_cols[2].markdown("**Distanz**")
    header_cols[3].markdown("**Von**")
    header_cols[4].markdown("**Bis**")
    header_cols[5].markdown("**Anzeige**")
    header_cols[6].markdown("**Loeschen**")

    for item in altersklassen:
        row_cols = st.columns([1, 3, 2, 2, 2, 3, 1])
        row_cols[0].number_input(
            "Sort.",
            min_value=0,
            max_value=999,
            value=item.sortierung or 0,
            step=1,
            key=f"ak_sortierung_{item.id}",
            label_visibility="collapsed",
        )
        row_cols[1].text_input(
            "Klasse",
            value=item.name,
            key=f"ak_name_{item.id}",
            label_visibility="collapsed",
        )
        row_cols[2].number_input(
            "Distanz in m",
            min_value=0,
            max_value=9999,
            value=int(normalize_distance_number(item.distanz) or 0),
            step=1,
            key=f"ak_distanz_{item.id}",
            label_visibility="collapsed",
        )
        row_cols[3].number_input(
            "Von",
            min_value=0,
            max_value=2100,
            value=item.jahr_von,
            step=1,
            key=f"ak_von_{item.id}",
            label_visibility="collapsed",
        )
        row_cols[4].number_input(
            "Bis",
            min_value=0,
            max_value=2100,
            value=item.jahr_bis,
            step=1,
            key=f"ak_bis_{item.id}",
            label_visibility="collapsed",
        )
        row_cols[5].write(altersklasse_label(item))
        if row_cols[6].button("X", key=f"delete_altersklasse_{item.id}", help="Altersklasse loeschen"):
            db.delete(item)
            db.commit()
            st.success("Altersklasse wurde geloescht.")
            refresh()

    save_col, reset_col = st.columns([1, 1])
    if save_col.button("Altersklassen speichern", type="primary"):
        valid = True
        for item in altersklassen:
            altersklasse = db.query(Altersklasse).get(item.id)
            if not altersklasse:
                continue
            name = st.session_state.get(f"ak_name_{item.id}", "").strip()
            jahr_von = int(st.session_state.get(f"ak_von_{item.id}", item.jahr_von))
            jahr_bis = int(st.session_state.get(f"ak_bis_{item.id}", item.jahr_bis))
            if not name or jahr_von > jahr_bis:
                valid = False
                continue
            altersklasse.name = name
            altersklasse.distanz = normalize_distance_number(st.session_state.get(f"ak_distanz_{item.id}", ""))
            altersklasse.jahr_von = jahr_von
            altersklasse.jahr_bis = jahr_bis
            altersklasse.sortierung = int(st.session_state.get(f"ak_sortierung_{item.id}", item.sortierung or 0))
        if valid:
            db.commit()
            st.success("Altersklassen wurden gespeichert.")
            refresh()
        else:
            db.rollback()
            st.error("Bitte pruefe Name und Jahrgangsbereich der Altersklassen.")

    if reset_col.button("Standard-Klasseneinteilung wiederherstellen"):
        db.query(Altersklasse).delete(synchronize_session=False)
        db.commit()
        create_default_altersklassen()
        st.success("Standard-Klasseneinteilung wurde wiederhergestellt.")
        refresh()


def render_startlist_competitions(db, bewerbe, empty_message, run_numbers):
    if not bewerbe:
        st.info(empty_message)
        return
    for bewerb in bewerbe:
        with st.expander(f"Bewerb ID {bewerb.id}: {bewerb.full_name()}", expanded=True):
            is_relay = is_staffel_bewerb(bewerb)
            has_times = bewerb_has_times(bewerb)
            if has_times:
                st.warning("Startliste gesperrt: Fuer diesen Bewerb wurden bereits Zeiten erfasst.")
            if st.button(
                "Startliste aktualisieren",
                key=f"refresh_startlist_{bewerb.id}",
                disabled=has_times,
            ):
                generate_runs_for_bewerb(bewerb, db, replace=True)
                st.success(f"Startliste fuer {bewerb.full_name()} wurde aktualisiert.")
                refresh()

            if is_relay:
                relay_names = sorted(
                    {
                        participant.staffel.strip()
                        for participant in db.query(Teilnehmer).filter(Teilnehmer.staffel != "").all()
                        if participant.staffel and participant.staffel.strip()
                    }
                )
                if not relay_names:
                    st.info("Keine Staffeln vorhanden. Trage bei Teilnehmenden einen Staffelnamen ein.")
                    continue
                if not bewerb.laufe:
                    st.write(f"{len(relay_names)} Staffeln, noch keine Laeufe erzeugt.")
                    continue
            elif not bewerb.anmeldungen:
                st.info("Keine Anmeldungen fuer diesen Bewerb.")
                continue

            if not bewerb.laufe:
                st.write(f"{len(bewerb.anmeldungen)} Anmeldungen, noch keine Laeufe erzeugt.")
                continue

            for lauf in sorted(bewerb.laufe, key=lambda item: item.laufnummer):
                st.markdown(f"**Lauf {run_numbers[lauf.id]}**")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Bahn": bahn.bahn,
                                "Staffel" if is_relay else "Teilnehmer": (
                                    bahn.teilnehmer.staffel
                                    if is_relay and bahn.teilnehmer
                                    else bahn.teilnehmer.display_name()
                                    if bahn.teilnehmer
                                    else "-"
                                ),
                            }
                            for bahn in sorted(lauf.laufbahnen, key=lambda item: item.bahn)
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )


def page_startliste(db):
    st.title("Startliste")
    bewerbe = (
        db.query(Bewerb)
        .options(
            joinedload(Bewerb.jahrgang),
            joinedload(Bewerb.anmeldungen).joinedload(Anmeldung.teilnehmer),
            joinedload(Bewerb.laufe)
            .joinedload(Lauf.laufbahnen)
            .joinedload(LaufBahn.teilnehmer),
        )
        .order_by(Bewerb.id)
        .all()
    )

    action_col1, action_col2 = st.columns(2)
    if action_col1.button("Startlisten erzeugen", type="primary"):
        for bewerb in bewerbe:
            generate_runs_for_bewerb(bewerb, db)
        st.success("Startlisten wurden erzeugt.")
        refresh()

    if action_col2.button("Alle Startlisten aktualisieren"):
        skipped = 0
        for bewerb in bewerbe:
            if bewerb_has_times(bewerb):
                skipped += 1
                continue
            generate_runs_for_bewerb(bewerb, db, replace=True)
        st.success("Startlisten ohne erfasste Zeiten wurden aktualisiert.")
        if skipped:
            st.warning(f"{skipped} Bewerb(e) wurden uebersprungen, weil bereits Zeiten erfasst sind.")
        refresh()

    st.download_button(
        "Startliste als PDF herunterladen",
        data=build_startlist_pdf(bewerbe),
        file_name="startliste-aushang.pdf",
        mime="application/pdf",
    )

    einzel_bewerbe = [bewerb for bewerb in bewerbe if not is_staffel_bewerb(bewerb)]
    staffel_bewerbe = [bewerb for bewerb in bewerbe if is_staffel_bewerb(bewerb)]
    run_numbers = global_run_numbers(bewerbe)

    einzel_tab, staffel_tab = st.tabs(["Einzelbewerbe", "Staffel"])
    with einzel_tab:
        render_startlist_competitions(db, einzel_bewerbe, "Keine Einzelbewerbe vorhanden.", run_numbers)
    with staffel_tab:
        render_startlist_competitions(db, staffel_bewerbe, "Keine Staffelbewerbe vorhanden.", run_numbers)


def render_timekeeping_runs(laufe, empty_message, run_numbers):
    if not laufe:
        st.info(empty_message)
        return
    for lauf in laufe:
        st.subheader(f"Bewerb ID {lauf.bewerb.id}: {lauf.bewerb.full_name()} - Lauf {run_numbers[lauf.id]}")

        for bahn in sorted(lauf.laufbahnen, key=lambda item: item.bahn):
            lane_col, name_col, time_col = st.columns([1, 3, 2])
            lane_col.write(f"Bahn {bahn.bahn}")
            if is_staffel_bewerb(lauf.bewerb) and bahn.teilnehmer:
                name_col.write(f"Staffel {bahn.teilnehmer.staffel or '-'}")
            else:
                name_col.write(bahn.teilnehmer.display_name() if bahn.teilnehmer else "-")
            time_key = f"time_{bahn.id}"
            if time_key not in st.session_state:
                st.session_state[time_key] = format_time_input(bahn.zeit_ms)
            time_col.text_input(
                "MM:SS:MS",
                placeholder="011520",
                key=time_key,
                on_change=save_lane_time,
                args=(bahn.id, time_key),
                label_visibility="collapsed",
            )


def page_zeitnehmung(db):
    st.title("Zeitnehmung")

    staffel_bewerbe = [bewerb for bewerb in db.query(Bewerb).all() if is_staffel_bewerb(bewerb)]
    for bewerb in staffel_bewerbe:
        if not bewerb.laufe:
            generate_runs_for_bewerb(bewerb, db)

    laufe = (
        db.query(Lauf)
        .options(
            joinedload(Lauf.bewerb).joinedload(Bewerb.jahrgang),
            joinedload(Lauf.laufbahnen).joinedload(LaufBahn.teilnehmer),
        )
        .order_by(Lauf.bewerb_id, Lauf.id)
        .all()
    )

    if not laufe:
        st.info("Keine Laeufe vorhanden. Erzeuge zuerst Startlisten.")
        return

    staffel_laufe = [lauf for lauf in laufe if is_staffel_bewerb(lauf.bewerb)]
    einzel_laufe = [lauf for lauf in laufe if not is_staffel_bewerb(lauf.bewerb)]
    run_numbers = {
        lauf.id: number
        for number, lauf in enumerate(
            sorted(laufe, key=lambda item: (item.bewerb_id, item.laufnummer, item.id)),
            start=1,
        )
    }

    einzel_tab, staffel_tab = st.tabs(["Einzelbewerbe", "Staffel"])
    with einzel_tab:
        render_timekeeping_runs(einzel_laufe, "Keine Einzel-Laeufe vorhanden.", run_numbers)
    with staffel_tab:
        render_timekeeping_runs(
            staffel_laufe,
            "Keine Staffel-Laeufe vorhanden. Lege Staffeln bei den Teilnehmenden an.",
            run_numbers,
        )


def page_ergebnisse(db):
    st.title("Ergebnisse")
    if st.button("Ergebnisse aktualisieren", type="primary"):
        refresh()

    results = build_results(db)

    export_col1, export_col2 = st.columns(2)
    export_col1.download_button(
        "Ergebnisse als CSV herunterladen",
        data=results_csv(results),
        file_name="ergebnisse.csv",
        mime="text/csv",
    )
    export_col2.download_button(
        "Ergebnisse als PDF herunterladen",
        data=build_results_pdf(results),
        file_name="ergebnisse-ortsmeisterschaften.pdf",
        mime="application/pdf",
    )

    for bewerb_data in results:
        st.subheader(bewerb_data["bewerb"].full_name())
        if not bewerb_data["results"]:
            st.info("Noch keine Zeiten vorhanden.")
            continue
        for group in bewerb_data["altersklassen"]:
            group_label = result_group_label(bewerb_data["bewerb"], group)
            st.markdown(f"**{group_label}**")
            rows = [
                {
                    "Platz": rank,
                    "Altersklasse": group_label,
                    "Teilnehmer": item["teilnehmer"].display_name(),
                    "Lauf": item["lauf"].laufnummer,
                    "Bahn": item["bahn"].bahn,
                    "Zeit": format_ms(item["zeit_ms"]),
                }
                for rank, item in enumerate(group["results"], start=1)
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def page_staffelwertung(db):
    st.title("Staffelwertung")
    st.caption("Gewinner ist die Staffel, deren Gesamtzeit am naechsten an der Durchschnittszeit aller Staffeln liegt.")

    if st.button("Staffelwertung aktualisieren", type="primary"):
        refresh()

    relay_rows, average_ms = build_relay_results(db)
    if not relay_rows:
        st.info("Keine Staffelzeiten vorhanden. Teilnehmende brauchen einen Staffelnamen und gespeicherte Zeiten.")
        return

    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Staffeln", len(relay_rows))
    metric_col2.metric("Durchschnittszeit", format_ms(average_ms))
    metric_col3.metric("Gewinner", relay_rows[0]["staffel"])

    ranking_rows = [
        {
            "Rang": index,
            "Staffel": item["staffel"],
            "Gesamtzeit": format_ms(item["zeit_ms"]),
            "Abweichung": format_ms(item["abweichung_ms"]),
            "Teilnehmer": ", ".join(item["teilnehmer"]),
            "Starts": len(item["starts"]),
        }
        for index, item in enumerate(relay_rows, start=1)
    ]
    st.dataframe(pd.DataFrame(ranking_rows), use_container_width=True, hide_index=True)

    st.subheader("Details")
    for index, item in enumerate(relay_rows, start=1):
        with st.expander(f"{index}. {item['staffel']} - Abweichung {format_ms(item['abweichung_ms'])}"):
            st.write(f"Gesamtzeit: {format_ms(item['zeit_ms'])}")
            st.write(f"Teilnehmer: {', '.join(item['teilnehmer'])}")
            st.dataframe(pd.DataFrame(item["starts"]), use_container_width=True, hide_index=True)


def page_ortsmeister(db):
    st.title("Ortsmeister")
    st.caption("Gewertet werden nur Personen, die in allen als Ortsmeister relevant markierten Bewerben eine Zeit haben.")

    bewerbe = (
        db.query(Bewerb)
        .options(joinedload(Bewerb.jahrgang))
        .order_by(Bewerb.id)
        .all()
    )
    male_bewerbe = [
        bewerb
        for bewerb in bewerbe
        if not is_staffel_bewerb(bewerb)
        and bool(bewerb.ortsmeister_relevant)
        and normalized_gender(bewerb.geschlecht) in {"maennlich", "mixed"}
    ]
    female_bewerbe = [
        bewerb
        for bewerb in bewerbe
        if not is_staffel_bewerb(bewerb)
        and bool(bewerb.ortsmeister_relevant)
        and normalized_gender(bewerb.geschlecht) in {"weiblich", "mixed"}
    ]
    male_ids = [bewerb.id for bewerb in male_bewerbe]
    female_ids = [bewerb.id for bewerb in female_bewerbe]

    if not male_ids and not female_ids:
        st.info("Markiere in Einstellungen mindestens einen Einzelbewerb als Ortsmeister relevant.")
        return

    male_results = build_ortsmeister_results(db, male_ids, "maennlich")
    female_results = build_ortsmeister_results(db, female_ids, "weiblich")

    male_tab, female_tab = st.tabs(["Maennlich", "Weiblich"])
    for tab, title, rows, relevant_bewerbe in [
        (male_tab, "Ortsmeister Maennlich", male_results, male_bewerbe),
        (female_tab, "Ortsmeister Weiblich", female_results, female_bewerbe),
    ]:
        with tab:
            if relevant_bewerbe:
                st.write("Relevante Bewerbe:")
                st.dataframe(
                    pd.DataFrame(
                        [{"ID": bewerb.id, "Bewerb": bewerb.full_name()} for bewerb in relevant_bewerbe]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("Keine Bewerbe fuer diese Kategorie markiert.")
                continue
            if rows:
                st.metric(title, rows[0]["teilnehmer"].display_name())
                table_rows = []
                for rank, item in enumerate(rows, start=1):
                    row = {
                        "Rang": rank,
                        "Teilnehmer": item["teilnehmer"].display_name(),
                        "Gesamtzeit": format_ms(item["gesamt_ms"]),
                    }
                    for time_item in item["zeiten"]:
                        row[f"ID {time_item['bewerb'].id}"] = format_ms(time_item["zeit_ms"])
                    table_rows.append(row)
                st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
            else:
                st.info("Keine vollstaendige Wertung fuer diese Kategorie.")


def page_tagesschnellste(db):
    st.title("Tagesschnellste")
    st.caption("Schnellste Zeiten auf 50m und 100m, getrennt nach Maennlich und Weiblich. Gaeste werden mitgewertet.")

    groups = build_day_fastest_results(db)
    distance_tabs = st.tabs(["50m", "100m"])
    for tab, distance in zip(distance_tabs, ["50m", "100m"]):
        with tab:
            male_col, female_col = st.columns(2)
            for column, title, gender in [
                (male_col, "Maennlich", "maennlich"),
                (female_col, "Weiblich", "weiblich"),
            ]:
                rows = groups[distance][gender]
                with column:
                    st.subheader(title)
                    if rows:
                        winner = rows[0]
                        st.metric("Schnellste Zeit", format_ms(winner["zeit_ms"]), winner["teilnehmer"].display_name())
                        table_rows = [
                            {
                                "Rang": index,
                                "Teilnehmer": item["teilnehmer"].display_name(),
                                "Gast": "ja" if item["teilnehmer"].gast else "nein",
                                "Bewerb": f"ID {item['bewerb'].id}: {item['bewerb'].full_name()}",
                                "Lauf": item["lauf"],
                                "Bahn": item["bahn"],
                                "Zeit": format_ms(item["zeit_ms"]),
                            }
                            for index, item in enumerate(rows, start=1)
                        ]
                        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
                    else:
                        st.info("Keine Zeiten vorhanden.")


def page_urkunden(db):
    st.title("Urkunden")
    st.caption("Serienbrief fuer vorgedruckte Urkunden. Das PDF enthaelt nur die einzudruckenden Textfelder.")

    results = build_results(db)
    defaults = certificate_field_defaults()
    initialize_certificate_template_state(db, defaults)
    apply_pending_certificate_template(defaults)
    selection_col, date_col = st.columns([1, 1])
    bewerb_options = {
        f"Bewerb ID {bewerb_data['bewerb'].id}: {bewerb_data['bewerb'].full_name()}": bewerb_data
        for bewerb_data in results
        if not is_staffel_bewerb(bewerb_data["bewerb"])
    }
    altersklasse_options = {}
    for bewerb_data in results:
        if is_staffel_bewerb(bewerb_data["bewerb"]):
            continue
        for group in bewerb_data["altersklassen"]:
            if not group["results"]:
                continue
            label = (
                f"Bewerb ID {bewerb_data['bewerb'].id}: "
                f"{bewerb_data['bewerb'].full_name()} - {result_group_label(bewerb_data['bewerb'], group)}"
            )
            altersklasse_options[label] = {"bewerb": bewerb_data["bewerb"], "group": group}
    certificate_options = ["Alle", "Ortsmeister", "Staffel"] + list(bewerb_options.keys()) + list(altersklasse_options.keys())
    scope = selection_col.selectbox("Urkunden fuer", certificate_options)
    date_col.text_input("Datum/Ort", key="certificate_date_text")
    if scope == "Alle":
        rows = certificate_rows(results, None) + ortsmeister_certificate_rows(db) + relay_certificate_rows(db)
    elif scope == "Ortsmeister":
        rows = ortsmeister_certificate_rows(db)
    elif scope == "Staffel":
        rows = relay_certificate_rows(db)
    elif scope in altersklasse_options:
        option = altersklasse_options[scope]
        rows = certificate_rows(
            [
                {
                    "bewerb": option["bewerb"],
                    "results": option["group"]["results"],
                    "altersklassen": [option["group"]],
                }
            ],
            None,
        )
    else:
        rows = certificate_rows([bewerb_options[scope]], None)

    controls_col, preview_col = st.columns([1, 1])
    with controls_col:
        st.subheader("Druckposition")
        field_tabs = st.tabs(list(defaults.keys()))
        for tab, (label, config) in zip(field_tabs, defaults.items()):
            prefix = config["prefix"]
            with tab:
                st.checkbox("Feld drucken", key=f"{prefix}_visible")
                center_cols = st.columns(3)
                center_cols[0].button(
                    "Horizontal zentrieren",
                    key=f"center_x_{prefix}",
                    on_click=center_certificate_field,
                    args=(prefix, "x"),
                )
                center_cols[1].button(
                    "Vertikal zentrieren",
                    key=f"center_y_{prefix}",
                    on_click=center_certificate_field,
                    args=(prefix, "y"),
                )
                center_cols[2].button(
                    "Feld zentrieren",
                    key=f"center_both_{prefix}",
                    on_click=center_certificate_field,
                    args=(prefix, "both"),
                )
                st.slider(
                    f"{label}: X von links",
                    0.0,
                    210.0,
                    key=f"{prefix}_x",
                    step=0.5,
                    format="%.1f mm",
                )
                st.slider(
                    f"{label}: Y von oben",
                    0.0,
                    297.0,
                    key=f"{prefix}_y",
                    step=0.5,
                    format="%.1f mm",
                )
                st.slider(f"{label}: Schriftgroesse", 8, 48, key=f"{prefix}_size")

    settings = collect_certificate_template_settings(defaults)

    with controls_col:
        st.subheader("Vorlage")
        template_cols = st.columns(3)
        if template_cols[0].button("Vorlage speichern", type="primary"):
            save_certificate_template(db, settings)
            st.success("Urkundenvorlage wurde gespeichert.")
        if template_cols[1].button("Vorlage laden"):
            template = load_certificate_template(db)
            if template:
                st.session_state["pending_certificate_template"] = template
                st.success("Urkundenvorlage wurde geladen.")
                refresh()
            else:
                st.info("Noch keine gespeicherte Vorlage vorhanden.")
        if template_cols[2].button("Standardwerte"):
            st.session_state["pending_certificate_template"] = {}
            st.success("Standardwerte wurden geladen.")
            refresh()

    with st.expander("Manuelle Urkunde fuer Notfaelle"):
        manual_col1, manual_col2 = st.columns(2)
        manual_name = manual_col1.text_input("Name", key="manual_certificate_name")
        manual_competition = manual_col2.text_input("Bewerb", key="manual_certificate_competition")
        manual_age_class = manual_col1.text_input("Altersklasse", key="manual_certificate_age_class")
        manual_time = manual_col2.text_input("Zeit", key="manual_certificate_time", placeholder="01:15.20")
        manual_place = manual_col1.number_input(
            "Platz",
            min_value=1,
            max_value=999,
            value=1,
            step=1,
            key="manual_certificate_place",
        )
        manual_rows = [
            {
                "place": int(manual_place),
                "name": manual_name.strip() or "Name",
                "competition": manual_competition.strip() or "Bewerb",
                "age_class": manual_age_class.strip(),
                "time": manual_time.strip(),
                "copy": 1,
            }
        ]
        st.download_button(
            "Manuelle Urkunde als PDF herunterladen",
            data=build_certificates_pdf(manual_rows, settings),
            file_name="urkunde-manuell.pdf",
            mime="application/pdf",
        )

    with preview_col:
        st.subheader("PDF Vorschau")
        render_certificate_pdf_preview(rows[0] if rows else None, settings)

    if rows:
        st.download_button(
            "Urkunden Serienbrief als PDF herunterladen",
            data=build_certificates_pdf(rows, settings),
            file_name="urkunden-serienbrief.pdf",
            mime="application/pdf",
            type="primary",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Platz": row["place"],
                        "Teilnehmer": row["name"],
                        "Bewerb": row["competition"],
                        "Altersklasse": row.get("age_class", ""),
                        "Zeit": row["time"],
                        "Kopie": row.get("copy", 1),
                    }
                    for row in rows
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Keine Ergebnisse fuer Urkunden vorhanden.")


def page_datensicherung(db):
    st.title("Datensicherung")
    st.download_button(
        "Eingangsdaten zwischenspeichern",
        data=export_backup(db),
        file_name="ortsmeisterschaft-zwischenspeicher.json",
        mime="application/json",
        type="primary",
    )

    uploaded_backup = st.file_uploader("Zwischenspeicher laden", type=["json"])
    restore_confirmed = st.checkbox("Aktuelle Daten durch den Zwischenspeicher ersetzen")
    if st.button("Zwischenspeicher wiederherstellen", disabled=uploaded_backup is None or not restore_confirmed):
        try:
            data = json.loads(uploaded_backup.getvalue().decode("utf-8"))
            restore_backup(db, data)
            st.success("Zwischenspeicher wurde wiederhergestellt.")
            refresh()
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            st.error(f"Zwischenspeicher konnte nicht geladen werden: {exc}")


def main():
    init_database()
    db = get_db()
    try:
        st.sidebar.title("Navigation")
        if LOGO_PATH.exists():
            st.sidebar.image(str(LOGO_PATH), use_container_width=True)
        st.sidebar.download_button(
            "Zwischenspeichern",
            data=export_backup(db),
            file_name="ortsmeisterschaft-zwischenspeicher.json",
            mime="application/json",
        )
        page = st.sidebar.radio(
            "Bereich",
            [
                "Dashboard",
                "Einstellungen",
                "Altersklassen",
                "Anmeldung",
                "Startliste",
                "Zeitnehmung",
                "Ergebnisse",
                "Ortsmeister",
                "Tagesschnellste",
                "Staffelwertung",
                "Urkunden",
                "Datensicherung",
            ],
            label_visibility="collapsed",
        )

        if page == "Dashboard":
            page_home(db)
        elif page == "Anmeldung":
            page_anmeldung(db)
        elif page == "Einstellungen":
            page_settings(db)
        elif page == "Altersklassen":
            page_altersklassen(db)
        elif page == "Startliste":
            page_startliste(db)
        elif page == "Zeitnehmung":
            page_zeitnehmung(db)
        elif page == "Ergebnisse":
            page_ergebnisse(db)
        elif page == "Ortsmeister":
            page_ortsmeister(db)
        elif page == "Tagesschnellste":
            page_tagesschnellste(db)
        elif page == "Staffelwertung":
            page_staffelwertung(db)
        elif page == "Urkunden":
            page_urkunden(db)
        elif page == "Datensicherung":
            page_datensicherung(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()

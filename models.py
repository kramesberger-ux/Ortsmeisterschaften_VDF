import os
import re

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE_URL = "sqlite:///" + os.path.join(BASE_DIR, "database.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Jahrgang(Base):
    __tablename__ = "jahrgang"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    jahr_von = Column(Integer, nullable=False)
    jahr_bis = Column(Integer, nullable=False)
    bewerbe = relationship("Bewerb", back_populates="jahrgang", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Jahrgang {self.name}>"


class Bewerb(Base):
    __tablename__ = "bewerb"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    stil = Column(String(50), nullable=False)
    geschlecht = Column(String(50), nullable=False)
    distanz = Column(String(50), nullable=False)
    ortsmeister_relevant = Column(Boolean, default=False)
    ortsmeister_maennlich = Column(Boolean, default=False)
    ortsmeister_weiblich = Column(Boolean, default=False)
    jahrgang_id = Column(Integer, ForeignKey("jahrgang.id"), nullable=False)
    jahrgang = relationship("Jahrgang", back_populates="bewerbe")
    anmeldungen = relationship("Anmeldung", back_populates="bewerb", cascade="all, delete-orphan")
    laufe = relationship("Lauf", back_populates="bewerb", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Bewerb {self.name}>"

    def full_name(self):
        return f"{self.distanz} {self.stil} {self.geschlecht} ({self.jahrgang.name})"


class Teilnehmer(Base):
    __tablename__ = "teilnehmer"

    id = Column(Integer, primary_key=True)
    vorname = Column(String(100), nullable=False)
    nachname = Column(String(100), nullable=False)
    geburtsjahr = Column(Integer, nullable=False)
    geschlecht = Column(String(10), nullable=False)
    brust = Column(Boolean, default=False)
    freistil = Column(Boolean, default=False)
    staffel = Column(String(100), default="")
    anmeldungen = relationship("Anmeldung", back_populates="teilnehmer", cascade="all, delete-orphan")
    laufbahnen = relationship("LaufBahn", back_populates="teilnehmer")

    def __repr__(self):
        return f"<Teilnehmer {self.vorname} {self.nachname}>"

    def display_name(self):
        return f"{self.vorname} {self.nachname}"


class Anmeldung(Base):
    __tablename__ = "anmeldung"

    id = Column(Integer, primary_key=True)
    teilnehmer_id = Column(Integer, ForeignKey("teilnehmer.id"), nullable=False)
    bewerb_id = Column(Integer, ForeignKey("bewerb.id"), nullable=False)
    teilnehmer = relationship("Teilnehmer", back_populates="anmeldungen")
    bewerb = relationship("Bewerb", back_populates="anmeldungen")

    def __repr__(self):
        return f"<Anmeldung T{self.teilnehmer_id} B{self.bewerb_id}>"


class Lauf(Base):
    __tablename__ = "lauf"

    id = Column(Integer, primary_key=True)
    bewerb_id = Column(Integer, ForeignKey("bewerb.id"), nullable=False)
    laufnummer = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False, default="offen")
    bewerb = relationship("Bewerb", back_populates="laufe")
    laufbahnen = relationship("LaufBahn", back_populates="lauf", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Lauf {self.bewerb_id}-{self.laufnummer}>"


class LaufBahn(Base):
    __tablename__ = "laufbahn"

    id = Column(Integer, primary_key=True)
    lauf_id = Column(Integer, ForeignKey("lauf.id"), nullable=False)
    bahn = Column(Integer, nullable=False)
    teilnehmer_id = Column(Integer, ForeignKey("teilnehmer.id"), nullable=True)
    zeit_ms = Column(Integer, default=0)
    lauf = relationship("Lauf", back_populates="laufbahnen")
    teilnehmer = relationship("Teilnehmer", back_populates="laufbahnen")

    def __repr__(self):
        return f"<LaufBahn {self.lauf_id}-{self.bahn}>"

    def format_time(self):
        return format_ms(self.zeit_ms)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key = Column(String(100), primary_key=True)
    value = Column(String(255), nullable=False)


Base.metadata.create_all(bind=engine)


def create_sample_data():
    db = SessionLocal()
    if db.query(AppMeta).filter_by(key="sample_data_initialized").first():
        db.close()
        return
    if db.query(Jahrgang).first() or db.query(Bewerb).first() or db.query(Teilnehmer).first():
        db.add(AppMeta(key="sample_data_initialized", value="1"))
        db.commit()
        db.close()
        return

    jahrgaenge = [
        Jahrgang(name="2011-2015", jahr_von=2011, jahr_bis=2015),
        Jahrgang(name="2010-1949", jahr_von=1949, jahr_bis=2010),
        Jahrgang(name="1950 und aelter", jahr_von=0, jahr_bis=1949),
    ]
    db.add_all(jahrgaenge)
    db.commit()

    bewerbe = [
        Bewerb(
            name="50m Brust weiblich",
            distanz="50m",
            stil="Brust",
            geschlecht="weiblich",
            jahrgang_id=jahrgaenge[0].id,
        ),
        Bewerb(
            name="100m Freistil maennlich",
            distanz="100m",
            stil="Freistil",
            geschlecht="maennlich",
            jahrgang_id=jahrgaenge[1].id,
        ),
        Bewerb(
            name="4x25m Staffel mixed",
            distanz="4x25m",
            stil="Freistil",
            geschlecht="mixed",
            jahrgang_id=jahrgaenge[2].id,
        ),
    ]
    db.add_all(bewerbe)
    db.add(AppMeta(key="sample_data_initialized", value="1"))
    db.commit()
    db.close()


def parse_bool(value):
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"ja", "j", "yes", "y", "true", "1", "wahr"}


def parse_time_to_ms(value):
    if not value:
        return 0
    value = value.strip().replace(",", ".")
    compact = re.sub(r"\D", "", value)
    if len(compact) == 6:
        minutes = int(compact[:2])
        seconds = int(compact[2:4])
        hundredths = int(compact[4:6])
        if seconds <= 59:
            return minutes * 60000 + seconds * 1000 + hundredths * 10

    match = re.match(r"^(?:(\d+):)?([0-5]?\d)(?:\.(\d{1,2}))?$", value)
    if not match:
        return 0
    minutes = int(match.group(1)) if match.group(1) else 0
    seconds = int(match.group(2))
    hundredths = int(match.group(3).ljust(2, "0")) if match.group(3) else 0
    return minutes * 60000 + seconds * 1000 + hundredths * 10


def format_ms(ms):
    if not ms:
        return ""
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    hundredths = (ms % 1000) // 10
    return f"{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def normalized_gender(value):
    value = value.strip().lower()
    if value in {"m", "male", "maennlich", "mannlich", "männlich", "mÃ¤nnlich"}:
        return "maennlich"
    if value in {"w", "f", "female", "weiblich"}:
        return "weiblich"
    if value == "mixed":
        return "mixed"
    return value


def gender_match(participant, bewerb):
    bewerb_gender = normalized_gender(bewerb.geschlecht)
    if bewerb_gender == "mixed":
        return True
    return normalized_gender(participant.geschlecht) == bewerb_gender


def is_staffel_bewerb(bewerb):
    text = f"{bewerb.name} {bewerb.stil} {bewerb.distanz}".lower()
    return "staffel" in text or "4x" in text


def style_match(participant, bewerb):
    if is_staffel_bewerb(bewerb):
        return bool(participant.staffel and participant.staffel.strip())
    if bewerb.stil.lower() == "brust":
        return participant.brust
    if bewerb.stil.lower() == "freistil":
        return participant.freistil
    return False


def assign_bewerbe_for_teilnehmer(teilnehmer, db):
    bewerbe = db.query(Bewerb).all()
    for bewerb in bewerbe:
        if (
            teilnehmer.geburtsjahr >= bewerb.jahrgang.jahr_von
            and teilnehmer.geburtsjahr <= bewerb.jahrgang.jahr_bis
            and gender_match(teilnehmer, bewerb)
            and style_match(teilnehmer, bewerb)
        ):
            existing = (
                db.query(Anmeldung)
                .filter_by(teilnehmer_id=teilnehmer.id, bewerb_id=bewerb.id)
                .first()
            )
            if not existing:
                db.add(Anmeldung(teilnehmer_id=teilnehmer.id, bewerb_id=bewerb.id))
    db.commit()


def split_run_sizes(count):
    if count <= 4:
        return [count]
    for runs in range(1, count + 1):
        if runs * 4 < count or runs * 2 > count:
            continue
        base = count // runs
        extra = count % runs
        sizes = [base + 1] * extra + [base] * (runs - extra)
        if all(2 <= size <= 4 for size in sizes):
            return sizes
    return [count]


def bewerb_has_times(bewerb):
    return any(
        bahn.zeit_ms
        for lauf in bewerb.laufe
        for bahn in lauf.laufbahnen
    )


def generate_runs_for_bewerb(bewerb, db, replace=False):
    participants = [anmeldung.teilnehmer for anmeldung in bewerb.anmeldungen]
    if replace and bewerb_has_times(bewerb):
        return bewerb.laufe
    if replace and bewerb.laufe:
        for lauf in list(bewerb.laufe):
            db.delete(lauf)
        db.flush()
        bewerb.laufe = []

    if is_staffel_bewerb(bewerb):
        all_relay_participants = (
            db.query(Teilnehmer)
            .filter(Teilnehmer.staffel != "")
            .order_by(Teilnehmer.id)
            .all()
        )
        participants_by_id = {participant.id: participant for participant in participants}
        for participant in all_relay_participants:
            participants_by_id.setdefault(participant.id, participant)
        participants = list(participants_by_id.values())
        teams = {}
        for participant in participants:
            relay_name = (participant.staffel or "").strip()
            if relay_name and relay_name not in teams:
                teams[relay_name] = participant
        participants = list(teams.values())
        if not participants:
            db.commit()
            return []

    if not participants:
        db.commit()
        return []
    if bewerb.laufe:
        return bewerb.laufe

    count = len(participants)
    sizes = split_run_sizes(count)
    index = 0
    laufe = []
    for nummer, size in enumerate(sizes, start=1):
        lauf = Lauf(bewerb_id=bewerb.id, laufnummer=nummer, status="offen")
        db.add(lauf)
        db.flush()
        for lane in range(1, size + 1):
            if index >= count:
                break
            teilnehmer = participants[index]
            db.add(LaufBahn(lauf_id=lauf.id, bahn=lane, teilnehmer_id=teilnehmer.id, zeit_ms=0))
            index += 1
        laufe.append(lauf)
    db.commit()
    return laufe

"""ATA 100 chapter names — the shared taxonomy across every OEM (PLAN §P1-M)."""

ATA_CHAPTERS = {
    "05": "Time Limits / Maintenance Checks",
    "06": "Dimensions and Areas", "07": "Lifting and Shoring",
    "08": "Leveling and Weighing", "09": "Towing and Taxiing",
    "10": "Parking and Mooring", "11": "Placards and Markings",
    "12": "Servicing",
    "20": "Standard Practices - Airframe",
    "21": "Air Conditioning", "22": "Auto Flight", "23": "Communications",
    "24": "Electrical Power", "25": "Equipment / Furnishings",
    "26": "Fire Protection", "27": "Flight Controls", "28": "Fuel",
    "29": "Hydraulic Power", "30": "Ice and Rain Protection",
    "31": "Indicating / Recording Systems", "32": "Landing Gear",
    "33": "Lights", "34": "Navigation", "35": "Oxygen", "36": "Pneumatic",
    "38": "Water / Waste", "42": "Integrated Modular Avionics",
    "44": "Cabin Systems", "45": "Central Maintenance System",
    "46": "Information Systems", "47": "Inert Gas System",
    "49": "Auxiliary Power Unit", "51": "Structures - General",
    "52": "Doors", "53": "Fuselage", "54": "Nacelles / Pylons",
    "55": "Stabilizers", "56": "Windows", "57": "Wings",
    "70": "Engine - Standard Practices", "71": "Power Plant",
    "72": "Engine", "73": "Engine Fuel and Control", "74": "Ignition",
    "75": "Air (Engine)", "76": "Engine Controls",
    "77": "Engine Indicating", "78": "Exhaust", "79": "Oil",
    "80": "Starting",
}


def chapter_name(ch: str) -> str:
    return ATA_CHAPTERS.get(ch, f"Chapter {ch}")


def build_embed_text(ch: str, sec: str, subj: str, title: str) -> str:
    """Revision-robust representation (Jo 2025): hierarchy + title, no body."""
    return f"{chapter_name(ch)} > {ch}-{sec}-{subj} > {title}"

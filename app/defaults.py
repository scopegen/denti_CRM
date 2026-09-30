"""What every new clinic starts with. Each clinic can edit its own copy afterwards."""

DEFAULT_PICKLISTS: dict[str, list[str]] = {
    # Dental relevant conditions: each changes how a doctor treats the patient.
    "medical_condition": [
        "Diabetes",
        "Hypertension (High Blood Pressure)",
        "Heart Disease",
        "Bleeding Disorder / Blood Thinners",
        "Asthma",
        "Epilepsy / Seizure Disorder",
        "Thyroid Disorder",
        "Pregnancy",
        "Allergy to Medication (e.g. Penicillin)",
        "Smoking / Tobacco Use",
    ],
    "payment_mode": ["Cash", "Card", "UPI"],
    # What a doctor marks on a tooth during the exam. "Missing" is separate, not in this list.
    "tooth_problem": [
        "Cavity",
        "Deep cavity",
        "Broken",
        "Needs root canal",
        "Loose",
        "Gum problem",
        "Sensitive",
        "Impacted",
        "Old filling",
    ],
}

# A starter catalog with typical prices (name, category, kind, price in rupees, recall months).
STARTER_SERVICES: list[tuple[str, str, str, int, int | None]] = [
    ("Scaling and polishing", "Cleaning", "in_house", 1500, 6),
    ("Tooth coloured filling", "Fillings", "in_house", 1500, None),
    ("Root canal treatment", "Root canal", "in_house", 6500, None),
    ("Re root canal treatment", "Root canal", "in_house", 8000, None),
    ("Extraction", "Extraction", "in_house", 1200, None),
    ("Wisdom tooth removal", "Extraction", "in_house", 4500, None),
    ("Ceramic crown", "Crowns and bridges", "lab", 9000, None),
    ("Metal ceramic crown", "Crowns and bridges", "lab", 6000, None),
    ("Complete denture", "Dentures", "lab", 15000, None),
    ("Dental implant", "Implants", "in_house", 32000, None),
    ("Teeth whitening", "Cosmetic", "in_house", 8000, None),
    ("Night guard", "Appliances", "lab", 3500, None),
]

# Finishing one of these marks the tooth as missing on the tooth chart.
TOOTH_REMOVING_SERVICES = {"Extraction", "Wisdom tooth removal"}

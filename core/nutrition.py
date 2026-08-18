"""
Open Food Facts payload -> flat, storable nutrition fields.

The OFF family of APIs punctuates nutriment keys inconsististently: some use
underscores (`sugars_100g`, `proteins_100g`) while others use hyphens
(`energy-kcal_100g`, `saturated-fat_100g`). Fields are also routinely absent --
`nova_group` is missing on plenty of real products -- so every value here is
optional and callers must treat None as "unknown", not zero.
"""

# Flat column name -> the nutriments key OFF actually uses.
NUTRIMENT_KEYS = {
    "kcal_100g": "energy-kcal_100g",
    "carbs_100g": "carbohydrates_100g",
    "sugars_100g": "sugars_100g",
    "fat_100g": "fat_100g",
    "sat_fat_100g": "saturated-fat_100g",
    "proteins_100g": "proteins_100g",
    "salt_100g": "salt_100g",
    "fiber_100g": "fiber_100g",
}

# USDA FoodData Central Nutrient IDs to our internal flat keys
FDC_NUTRIENT_MAP = {
    1008: "kcal_100g",       # Energy (kcal)
    1004: "fat_100g",        # Total lipid (fat)
    1258: "sat_fat_100g",    # Fatty acids, total saturated
    1005: "carbs_100g",      # Carbohydrate, by difference
    2000: "sugars_100g",     # Sugars, total including NLEA
    1010: "sugars_100g",     # Sugars, total
    1003: "proteins_100g",   # Protein
    1079: "fiber_100g",      # Fiber, total dietary
}

# Requested from the API. Kept in one place so every OFF-family call asks for the
# same set and the parser below never sees a field it did not request.
OFF_FIELDS = (
    "product_name,generic_name,product_name_en,brands,quantity,categories,"
    "image_front_small_url,image_front_url,nutriscore_grade,nova_group,"
    "allergens_tags,serving_size,nutriments"
)


def _num(value):
    """Coerces an OFF value to float, or None if absent/unparseable."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # OFF occasionally carries negative or absurd placeholder values
    return result if 0 <= result <= 100000 else None


def _int(value):
    """Coerces an OFF value to int, or None if absent/unparseable."""
    number = _num(value)
    return int(number) if number is not None else None


def clean_allergen_tags(tags):
    """
    Turns OFF allergen tags into a readable list.

    OFF returns locale-prefixed tags like ["en:nuts", "en:milk"]; the prefix is
    noise to a reader, so it is stripped and hyphens become spaces.
    """
    if not tags:
        return []

    cleaned = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        label = tag.split(":", 1)[1] if ":" in tag else tag
        label = label.replace("-", " ").strip()
        if label and label not in cleaned:
            cleaned.append(label)
    return cleaned


def parse_off_product(product):
    """
    Maps an OFF `product` object to flat fields.

    Returns a dict with the display fields and every nutriment column, using None
    for anything the product does not carry. Never raises on a malformed payload.
    """
    if not isinstance(product, dict):
        return {}

    nutriments = product.get("nutriments")
    if not isinstance(nutriments, dict):
        nutriments = {}

    parsed = {
        "quantity_label": (product.get("quantity") or "").strip() or None,
        "categories": (product.get("categories") or "").strip() or None,
        "image_url": product.get("image_front_small_url") or product.get("image_front_url") or None,
        "serving_size": (product.get("serving_size") or "").strip() or None,
        "nutriscore": (product.get("nutriscore_grade") or "").strip().lower() or None,
        "nova_group": _int(product.get("nova_group")),
        "allergens": ", ".join(clean_allergen_tags(product.get("allergens_tags"))) or None,
    }

    for column, off_key in NUTRIMENT_KEYS.items():
        parsed[column] = _num(nutriments.get(off_key))

    return parsed


def display_name(product):
    """
    Picks the best available product name from an OFF payload.

    Falls back through the generic and English names, since many regional entries
    fill only one of the three.
    """
    for key in ("product_name", "generic_name", "product_name_en"):
        value = product.get(key)
        if value and value.strip():
            return value.strip()
    return None


def parse_usda_product(food):
    """
    Maps a USDA FoodData Central branded food item payload to flat nutrition fields.
    """
    if not isinstance(food, dict):
        return {}

    parsed = {
        "quantity_label": None,
        "categories": (food.get("brandedFoodCategory") or "").strip() or None,
        "image_url": None,
        "serving_size": None,
        "nutriscore": None,
        "nova_group": None,
        "allergens": None,
    }

    # Format serving size if available
    serving_val = food.get("servingSize")
    serving_unit = food.get("servingSizeUnit")
    if serving_val is not None:
        parsed["serving_size"] = f"{serving_val} {serving_unit or ''}".strip()

    # Parse nutrients (which are normalized per 100g in FDC)
    nutrients = food.get("foodNutrients") or []
    for nut in nutrients:
        if not isinstance(nut, dict):
            continue
        nid = nut.get("nutrientId")
        val = _num(nut.get("value"))
        if val is None:
            continue

        if nid in FDC_NUTRIENT_MAP:
            key = FDC_NUTRIENT_MAP[nid]
            parsed[key] = val
        elif nid == 1093:  # Sodium (Na) in mg
            # Convert to Salt in grams: salt = sodium (mg) * 2.5 / 1000
            parsed["salt_100g"] = round(val * 0.0025, 4)

    return parsed


def has_nutrition(fields):
    """
    True when at least one nutriment value is present.

    Products resolved by non-food databases carry no nutrition at all, so views
    use this to decide between rendering a nutrition panel and showing a "no nutrition data" state.
    """
    return any(fields.get(column) is not None for column in NUTRIMENT_KEYS)

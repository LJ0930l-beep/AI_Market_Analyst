"""Independent readers for the explicit lossless model-facing evidence tables."""


def decode_price_action(technical, projected):
    encoding = technical.get("price_action_encoding")
    if not encoding:
        return projected

    def decode(value):
        if isinstance(value, dict):
            return {encoding["keys"].get(key, key): decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode(item) for item in value]
        if isinstance(value, str) and value.startswith("@") and value[1:].isdigit():
            return encoding["times"][int(value[1:])]
        return value

    assert "defaults" in encoding["rule"] and "@N" in encoding["rule"]
    return {**encoding["defaults"], **decode(projected)}


def decode_macro_releases(revision):
    encoding = revision.get("macro_release_encoding")
    if not encoding:
        return revision["macro_releases"]
    assert "columns" in encoding["rule"] and "strings[N]" in encoding["rule"]
    restored = []
    for row_index, row in enumerate(revision["macro_releases"]):
        missing = encoding["missing_columns"][row_index]
        restored.append({key: encoding["strings"][value["s"]] if isinstance(value, dict) else value
            for column, (key, value) in enumerate(zip(encoding["columns"], row)) if column not in missing})
    return restored

from typing import Any, List


def find_components_recursive(value: Any, component_name: str) -> List[dict]:
    """Find dictionaries with a matching Ozon layout component name."""
    matches = []

    if isinstance(value, dict):
        if value.get("component") == component_name:
            matches.append(value)
        for child in value.values():
            matches.extend(find_components_recursive(child, component_name))
    elif isinstance(value, list):
        for child in value:
            matches.extend(find_components_recursive(child, component_name))

    return matches

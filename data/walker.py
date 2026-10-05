"""Tiny tree walker used only by the data generator to compute gold paths/outcomes.

The production walker lives in pipeline/. Semantics: a None value at a visited node -> NEED_INFO.
"""

import operator

OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "in": lambda v, allowed: v in allowed,
}


def walk(tree: dict, values: dict) -> tuple[str, list[str]]:
    node, path = tree["root"], []
    while node in tree["nodes"]:
        spec = tree["nodes"][node]
        path.append(node)
        value = values.get(spec["var"])
        if value is None:
            return "NEED_INFO", path
        node = spec["yes"] if OPS[spec["op"]](value, spec["value"]) else spec["no"]
    return node, path


def enumerate_paths(tree: dict) -> list[tuple[list[str], str]]:
    """All root-to-leaf (node path, outcome) pairs."""
    out = []

    def dfs(node, path):
        if node not in tree["nodes"]:
            out.append((path, node))
            return
        spec = tree["nodes"][node]
        dfs(spec["yes"], path + [node])
        dfs(spec["no"], path + [node])

    dfs(tree["root"], [])
    return out

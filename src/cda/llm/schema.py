"""Convert Pydantic argument models to strict function schemas."""

from typing import Any

from cda.llm.types import ToolSpec


def strict_tool_schema(spec: ToolSpec) -> dict[str, Any]:
    schema = spec.args_model.model_json_schema()

    def nullable(node: dict[str, Any]) -> bool:
        if node.get("type") == "null":
            return True
        if "$ref" in node:
            target = schema
            for part in node["$ref"].removeprefix("#/").split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return nullable(target)
        return any(nullable(item) for item in node.get("anyOf", []))

    def convert(node: dict[str, Any]) -> None:
        if "default" in node and not nullable(node):
            raise ValueError("Strict tool fields with defaults must be nullable")
        node.pop("title", None)
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties", {})
            required = node.get("required", [])
            for name, child in properties.items():
                if name not in required and not nullable(child):
                    raise ValueError("Strict tool fields with defaults must be nullable")
            node["additionalProperties"] = False
            node["required"] = list(properties)
        for key, value in list(node.items()):
            if key in {"properties", "$defs", "patternProperties"}:
                for child in value.values():
                    convert(child)
            elif isinstance(value, dict):
                convert(value)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, dict):
                        convert(child)

    convert(schema)
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": schema,
            "strict": True,
        },
    }

"""Compact market tool results for LLM input without changing graph state."""
import csv
import re

from langchain_core.messages import AIMessage, ToolMessage


_DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}: ")


def _compact_stock_data(content: str) -> str:
    """Keep recent prices, historical extremes, and major price moves."""
    lines = content.splitlines()

    header_index = None
    columns = None

    for i, line in enumerate(lines):
        fields = next(csv.reader([line]))
        if "Date" in fields and "Close" in fields:
            header_index = i
            columns = fields
            break

    if header_index is None:
        return content

    data = []
    for line in lines[header_index + 1:]:
        if not line.strip():
            continue
        fields = next(csv.reader([line]))
        if len(fields) != len(columns):
            return content
        data.append(fields)

    if len(data) <= 25:
        return content

    def numeric(row, column):
        if column not in columns:
            return None
        try:
            return float(row[columns.index(column)])
        except (ValueError, TypeError):
            return None

    keep = set(range(min(3, len(data))))
    keep.update(range(max(0, len(data) - 15), len(data)))

    for column in ("High", "Low", "Close", "Volume"):
        values = [
            (i, numeric(row, column))
            for i, row in enumerate(data)
        ]
        values = [(i, v) for i, v in values if v is not None]

        if values:
            keep.add(min(values, key=lambda x: x[1])[0])
            keep.add(max(values, key=lambda x: x[1])[0])

    moves = []
    for i in range(1, len(data)):
        previous = numeric(data[i - 1], "Close")
        current = numeric(data[i], "Close")

        if previous and current is not None:
            moves.append((abs(current / previous - 1), i))

    if moves:
        biggest = max(moves)[1]
        keep.update((biggest - 1, biggest))

    if len(keep) == len(data):
        return content

    omitted = len(data) - len(keep)
    retained = [
        line
        for i, line in enumerate(lines[header_index + 1:])
        if i in keep
    ]

    return (
        "\n".join(lines[:header_index + 1])
        + f"\n# {omitted} historical rows omitted from LLM context; "
          "important price extremes retained.\n"
        + "\n".join(retained)
    )


def _compact_indicators(content: str) -> str:
    """Keep recent indicator values and a few earlier reference values."""
    lines = content.splitlines()
    dated = [
        i for i, line in enumerate(lines)
        if _DATE_VALUE.match(line)
    ]

    if len(dated) <= 13:
        return content

    # Indicator results are normally newest-first.
    keep = set(dated[:10] + dated[-3:])
    omitted = len(dated) - len(keep)

    result = []
    inserted_note = False

    for i, line in enumerate(lines):
        if i in dated and i not in keep:
            if not inserted_note:
                result.append(
                    f"[{omitted} intermediate dated values omitted "
                    "from LLM context]"
                )
                inserted_note = True
        else:
            result.append(line)

    return "\n".join(result)


def compact_market_messages(messages):
    """Return a compact model-input view; preserve original messages.

    Tool-call IDs and tool-call arguments remain unchanged.
    Verified market snapshots are never shortened.
    """
    tool_names = {}

    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call.get("id"):
                    tool_names[call["id"]] = call["name"]

    compacted = []

    for message in messages:
        if not isinstance(message, ToolMessage):
            compacted.append(message)
            continue

        name = message.name or tool_names.get(message.tool_call_id)
        content = message.content

        if not isinstance(content, str):
            compacted.append(message)
            continue

        if name == "get_stock_data":
            shortened = _compact_stock_data(content)
        elif name == "get_indicators":
            shortened = _compact_indicators(content)
        else:
            # Especially preserve get_verified_market_snapshot.
            shortened = content

        if shortened == content:
            compacted.append(message)
        else:
            compacted.append(
                message.model_copy(update={"content": shortened})
            )

    return compacted

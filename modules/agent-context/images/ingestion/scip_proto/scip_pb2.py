"""Generated SCIP protobuf definitions.

This module provides Python classes for decoding .scip index files.
Generated from: https://github.com/sourcegraph/scip/blob/main/scip.proto

The SCIP format encodes:
  - Index: top-level container with metadata + documents
  - Document: per-file occurrences and symbols
  - Occurrence: position + symbol + roles (definition, reference, etc.)
  - SymbolInformation: documentation for symbols

Usage:
    index = Index()
    index.ParseFromString(open("index.scip", "rb").read())
    for doc in index.documents:
        for occ in doc.occurrences:
            ...
"""

# We use a lightweight hand-rolled decoder to avoid requiring the full
# generated protobuf (which needs protoc at build time). The SCIP proto
# is stable and well-documented.

from dataclasses import dataclass, field


# SymbolRole bit flags (from scip.proto)
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE_ACCESS = 0x4
ROLE_READ_ACCESS = 0x8
ROLE_GENERATED = 0x10
ROLE_TEST = 0x20


@dataclass
class ToolInfo:
    name: str = ""
    version: str = ""
    arguments: list[str] = field(default_factory=list)


@dataclass
class Metadata:
    version: int = 0  # ProtocolVersion enum
    tool_info: ToolInfo = field(default_factory=ToolInfo)
    project_root: str = ""
    text_document_encoding: int = 0


@dataclass
class Relationship:
    symbol: str = ""
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False


@dataclass
class SymbolInformation:
    symbol: str = ""
    documentation: list[str] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)


@dataclass
class Occurrence:
    range: list[int] = field(default_factory=list)
    symbol: str = ""
    symbol_roles: int = 0
    override_documentation: list[str] = field(default_factory=list)
    syntax_kind: int = 0
    diagnostics: list = field(default_factory=list)


@dataclass
class Document:
    language: str = ""
    relative_path: str = ""
    occurrences: list[Occurrence] = field(default_factory=list)
    symbols: list[SymbolInformation] = field(default_factory=list)


@dataclass
class Index:
    metadata: Metadata = field(default_factory=Metadata)
    documents: list[Document] = field(default_factory=list)
    external_symbols: list[SymbolInformation] = field(default_factory=list)

    def ParseFromString(self, data: bytes) -> None:
        """Decode a .scip protobuf binary into this Index object."""
        _decode_index(data, self)


# ---------------------------------------------------------------------------
# Protobuf wire-format decoder (hand-rolled for SCIP schema stability)
# ---------------------------------------------------------------------------

# Wire types
VARINT = 0
FIXED64 = 1
LENGTH_DELIMITED = 2
FIXED32 = 5


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a varint from data at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError(f"Unexpected end of data at pos {pos}")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return result, pos


def _read_tag(data: bytes, pos: int) -> tuple[int, int, int]:
    """Read a protobuf tag. Returns (field_number, wire_type, new_pos)."""
    varint, pos = _read_varint(data, pos)
    field_number = varint >> 3
    wire_type = varint & 0x07
    return field_number, wire_type, pos


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    """Skip a field value based on wire type."""
    if wire_type == VARINT:
        _, pos = _read_varint(data, pos)
    elif wire_type == FIXED64:
        pos += 8
    elif wire_type == LENGTH_DELIMITED:
        length, pos = _read_varint(data, pos)
        pos += length
    elif wire_type == FIXED32:
        pos += 4
    else:
        raise ValueError(f"Unknown wire type: {wire_type}")
    return pos


def _decode_string(data: bytes, pos: int) -> tuple[str, int]:
    """Decode a length-delimited string."""
    length, pos = _read_varint(data, pos)
    value = data[pos : pos + length].decode("utf-8", errors="replace")
    return value, pos + length


def _decode_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    """Decode a length-delimited bytes field."""
    length, pos = _read_varint(data, pos)
    value = data[pos : pos + length]
    return value, pos + length


def _decode_packed_ints(data: bytes, pos: int) -> tuple[list[int], int]:
    """Decode a packed repeated int32/int64 field."""
    length, pos = _read_varint(data, pos)
    end = pos + length
    values = []
    while pos < end:
        val, pos = _read_varint(data, pos)
        # Handle zigzag encoding for signed ints
        values.append(val)
    return values, end


def _decode_tool_info(data: bytes) -> ToolInfo:
    """Decode a ToolInfo message."""
    info = ToolInfo()
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 1 and wire_type == LENGTH_DELIMITED:  # name
            info.name, pos = _decode_string(data, pos)
        elif field_num == 2 and wire_type == LENGTH_DELIMITED:  # version
            info.version, pos = _decode_string(data, pos)
        elif field_num == 3 and wire_type == LENGTH_DELIMITED:  # arguments
            arg, pos = _decode_string(data, pos)
            info.arguments.append(arg)
        else:
            pos = _skip_field(data, pos, wire_type)
    return info


def _decode_metadata(data: bytes) -> Metadata:
    """Decode a Metadata message."""
    meta = Metadata()
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 1 and wire_type == VARINT:  # version
            meta.version, pos = _read_varint(data, pos)
        elif field_num == 2 and wire_type == LENGTH_DELIMITED:  # tool_info
            msg_data, pos = _decode_bytes(data, pos)
            meta.tool_info = _decode_tool_info(msg_data)
        elif field_num == 3 and wire_type == LENGTH_DELIMITED:  # project_root
            meta.project_root, pos = _decode_string(data, pos)
        elif field_num == 4 and wire_type == VARINT:  # text_document_encoding
            meta.text_document_encoding, pos = _read_varint(data, pos)
        else:
            pos = _skip_field(data, pos, wire_type)
    return meta


def _decode_relationship(data: bytes) -> Relationship:
    """Decode a Relationship message."""
    rel = Relationship()
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 1 and wire_type == LENGTH_DELIMITED:  # symbol
            rel.symbol, pos = _decode_string(data, pos)
        elif field_num == 2 and wire_type == VARINT:  # is_reference
            val, pos = _read_varint(data, pos)
            rel.is_reference = bool(val)
        elif field_num == 3 and wire_type == VARINT:  # is_implementation
            val, pos = _read_varint(data, pos)
            rel.is_implementation = bool(val)
        elif field_num == 4 and wire_type == VARINT:  # is_type_definition
            val, pos = _read_varint(data, pos)
            rel.is_type_definition = bool(val)
        elif field_num == 5 and wire_type == VARINT:  # is_definition
            val, pos = _read_varint(data, pos)
            rel.is_definition = bool(val)
        else:
            pos = _skip_field(data, pos, wire_type)
    return rel


def _decode_symbol_information(data: bytes) -> SymbolInformation:
    """Decode a SymbolInformation message."""
    info = SymbolInformation()
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 1 and wire_type == LENGTH_DELIMITED:  # symbol
            info.symbol, pos = _decode_string(data, pos)
        elif field_num == 3 and wire_type == LENGTH_DELIMITED:  # documentation
            doc, pos = _decode_string(data, pos)
            info.documentation.append(doc)
        elif field_num == 4 and wire_type == LENGTH_DELIMITED:  # relationships
            msg_data, pos = _decode_bytes(data, pos)
            info.relationships.append(_decode_relationship(msg_data))
        else:
            pos = _skip_field(data, pos, wire_type)
    return info


def _decode_occurrence(data: bytes) -> Occurrence:
    """Decode an Occurrence message."""
    occ = Occurrence()
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 1 and wire_type == LENGTH_DELIMITED:  # range (packed int32)
            occ.range, pos = _decode_packed_ints(data, pos)
        elif field_num == 1 and wire_type == VARINT:  # range (unpacked, single value)
            val, pos = _read_varint(data, pos)
            occ.range.append(val)
        elif field_num == 2 and wire_type == LENGTH_DELIMITED:  # symbol
            occ.symbol, pos = _decode_string(data, pos)
        elif field_num == 3 and wire_type == VARINT:  # symbol_roles
            occ.symbol_roles, pos = _read_varint(data, pos)
        elif field_num == 4 and wire_type == LENGTH_DELIMITED:  # override_documentation
            doc, pos = _decode_string(data, pos)
            occ.override_documentation.append(doc)
        elif field_num == 5 and wire_type == VARINT:  # syntax_kind
            occ.syntax_kind, pos = _read_varint(data, pos)
        else:
            pos = _skip_field(data, pos, wire_type)
    return occ


def _decode_document(data: bytes) -> Document:
    """Decode a Document message."""
    doc = Document()
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 4 and wire_type == LENGTH_DELIMITED:  # language
            doc.language, pos = _decode_string(data, pos)
        elif field_num == 1 and wire_type == LENGTH_DELIMITED:  # relative_path
            doc.relative_path, pos = _decode_string(data, pos)
        elif field_num == 2 and wire_type == LENGTH_DELIMITED:  # occurrences
            msg_data, pos = _decode_bytes(data, pos)
            doc.occurrences.append(_decode_occurrence(msg_data))
        elif field_num == 3 and wire_type == LENGTH_DELIMITED:  # symbols
            msg_data, pos = _decode_bytes(data, pos)
            doc.symbols.append(_decode_symbol_information(msg_data))
        else:
            pos = _skip_field(data, pos, wire_type)
    return doc


def _decode_index(data: bytes, index: Index) -> None:
    """Decode the top-level Index message."""
    pos = 0
    end = len(data)
    while pos < end:
        field_num, wire_type, pos = _read_tag(data, pos)
        if field_num == 1 and wire_type == LENGTH_DELIMITED:  # metadata
            msg_data, pos = _decode_bytes(data, pos)
            index.metadata = _decode_metadata(msg_data)
        elif field_num == 2 and wire_type == LENGTH_DELIMITED:  # documents
            msg_data, pos = _decode_bytes(data, pos)
            index.documents.append(_decode_document(msg_data))
        elif field_num == 3 and wire_type == LENGTH_DELIMITED:  # external_symbols
            msg_data, pos = _decode_bytes(data, pos)
            index.external_symbols.append(_decode_symbol_information(msg_data))
        else:
            pos = _skip_field(data, pos, wire_type)

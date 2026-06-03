# Salesforce DX / SFDX repository support for graphify detect + extract pipelines.
#
# Coverage is driven by the Salesforce Metadata Registry (source-deploy-retrieve)
# plus path-aware rules for bundle layouts (lwc/, aura/, force-app/main/default/).
from __future__ import annotations

import importlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

# ── Registry-backed suffix list (MDAPI-style extensions under SF trees) ───────

_DATA_PATH = Path(__file__).parent / "data" / "salesforce_registry_suffixes.json"
_REGISTRY_SUFFIXES: tuple[str, ...] = tuple(
    json.loads(_DATA_PATH.read_text(encoding="utf-8"))
) if _DATA_PATH.is_file() else ()

SALESFORCE_REGISTRY_EXTENSIONS: frozenset[str] = frozenset(
    f".{suffix}" for suffix in _REGISTRY_SUFFIXES
)

# Apex, Aura, Visualforce — always code regardless of directory
SALESFORCE_CORE_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".cls",
    ".trigger",
    ".cmp",
    ".app",
    ".evt",
    ".intf",
    ".auradoc",
    ".design",
    ".page",
    ".component",
})

# Decomposed source uses *.{registrySuffix}-meta.xml (all registry types in source format)
_META_XML_RE = re.compile(r"\.[A-Za-z][\w]*-meta\.xml$", re.IGNORECASE)
_LWC_JS_META_SUFFIX = ".js-meta.xml"

# Directories that indicate Salesforce package content (case-insensitive match)
_SF_CONTENT_DIRS: frozenset[str] = frozenset({
    "classes", "triggers", "objects", "flows", "flowdefinitions",
    "lwc", "aura", "pages", "components", "permissionsets", "profiles",
    "layouts", "flexipages", "tabs", "applications", "custommetadata",
    "customlabels", "labels", "staticresources", "contentassets",
    "experiences", "digitalexperiences", "weblinks", "reports",
    "dashboards", "email", "emailservices", "workflows", "approvalprocesses",
    "queues", "groups", "roles", "territory2models", "namedcredentials",
    "connectedapps", "platformeventchannels", "genaiplugins", "bots",
    "entitlementprocesses", "sharingrules", "assignmentrules",
    "escalationrules", "autoresponserules", "matchingrules", "duplicaterules",
    "globalvaluesets", "standardvaluesets", "datacategorygroups",
    "documents", "documentfolders", "waveapplications", "wavedashboards",
})

_SF_PROJECT_FILES: frozenset[str] = frozenset({
    "sfdx-project.json",
    "project-scratch-def.json",
    "package.xml",
    "destructivechanges.xml",
    "destructivechangespost.xml",
    "destructivechangespre.xml",
})

# XML element local-names that denote cross-metadata references.
# "label" and "fullname" are intentionally excluded: they describe the
# component itself (display name / API name), not references to other
# components.  Using them as reference targets produces spurious edges.
_REFERENCE_TAGS: frozenset[str] = frozenset({
    "apexclass", "apexpage", "apexcomponent", "controller", "extension",
    "referenceto", "refto", "content", "customobject", "object", "objects",
    "flow", "flowname", "flowdefinition", "subflow", "field", "recordtype",
    "lightningcomponent", "lwccomponent", "page", "tab", "application",
    "profile", "permissionset", "permissionsets", "report", "dashboard",
    "emailtemplate", "template", "targetobject",
    "sobjecttype", "sobject", "extends", "implements", "contentasset",
    "resource", "namedcredential", "connectedapp", "customtab", "flexipage",
    "recordtype", "compactlayout", "listview", "validationrule",
    "workflowrule", "quickaction", "actionname", "actiontype", "target",
    "assignments", "role", "queue", "group", "territory2", "botversion",
    "entity", "custommetadata", "globalvalueset", "valueset", "layout",
    "reporttype", "flexipage", "customfield", "relationshipname",
    "workflowoutboundmessage", "embeddedservice", "certificate",
})

# XML attributes (local-name, lowercased) that hold metadata references
_REFERENCE_ATTRS: frozenset[str] = frozenset({
    "object", "sobject", "sobjecttype", "referenceTo", "class", "field",
    "tab", "page", "flow", "target", "targets", "entity", "content",
    "controller", "extension", "recordtype", "profile", "permissionset",
})

# Map from -meta.xml type suffix to a canonical sf_type string.
_SF_META_TYPE_MAP: dict[str, str] = {
    "flow": "flow",
    "flowdefinition": "flow",
    "object": "sobject",
    "field": "sobject_field",
    "permissionset": "permission_set",
    "permissionsetgroup": "permission_set",
    "profile": "profile",
    "layout": "layout",
    "flexipage": "flexipage",
    "apexclass": "apex_class",
    "apextrigger": "apex_trigger",
    "auradefinitionbundle": "aura_bundle",
    "lightningcomponentbundle": "lwc_bundle",
    "page": "visualforce_page",
    "component": "visualforce_component",
    "workflow": "workflow",
    "approvalprocess": "approval_process",
    "customtab": "tab",
    "compactlayout": "compact_layout",
    "listview": "list_view",
    "validationrule": "validation_rule",
    "recordtype": "record_type",
    "globalvalueset": "global_value_set",
    "standardvalueset": "value_set",
    "customlabels": "custom_labels",
    "connectedapp": "connected_app",
    "namedcredential": "named_credential",
    "remotesitesetting": "remote_site",
    "custommetadata": "custom_metadata",
    "report": "report",
    "dashboard": "dashboard",
    "emailtemplate": "email_template",
    "contentasset": "content_asset",
    "staticresource": "static_resource",
}

# Map from a _REFERENCE_TAGS key to the sf_type of its reference targets.
_TAG_TO_SF_TYPE: dict[str, str] = {
    "apexclass": "apex_class",
    "apexpage": "visualforce_page",
    "apexcomponent": "visualforce_component",
    "controller": "apex_class",
    "extension": "apex_class",
    "referenceto": "sobject",
    "customobject": "sobject",
    "object": "sobject",
    "objects": "sobject",
    "targetobject": "sobject",
    "sobjecttype": "sobject",
    "sobject": "sobject",
    "flow": "flow",
    "flowname": "flow",
    "flowdefinition": "flow",
    "subflow": "flow",
    "permissionset": "permission_set",
    "permissionsets": "permission_set",
    "profile": "profile",
    "lightningcomponent": "lwc_bundle",
    "lwccomponent": "lwc_bundle",
    "namedcredential": "named_credential",
    "connectedapp": "connected_app",
    "contentasset": "content_asset",
    "custommetadata": "custom_metadata",
    "globalvalueset": "global_value_set",
    "report": "report",
    "dashboard": "dashboard",
    "emailtemplate": "email_template",
    "flexipage": "flexipage",
}


def _sf_type_from_path(path: Path) -> str:
    """Infer the Salesforce component type from a file path."""
    ext = path.suffix.lower()
    if ext == ".cls":
        return "apex_class"
    if ext == ".trigger":
        return "apex_trigger"
    if ext == ".page":
        return "visualforce_page"
    if ext == ".component":
        return "visualforce_component"
    if ext in (".cmp", ".app", ".evt", ".intf", ".auradoc", ".design"):
        return "aura_bundle"
    name = path.name.lower()
    if name.endswith("-meta.xml"):
        base = name[: -len("-meta.xml")]
        if "." in base:
            metatype = base.rsplit(".", 1)[1]
            return _SF_META_TYPE_MAP.get(metatype, metatype)
    if _in_lwc(path):
        return "lwc_bundle"
    if "aura" in {p.lower() for p in path.parts}:
        return "aura_bundle"
    return "metadata"


_APEX_CLASS_RE = re.compile(
    r"^\s*(?:global|public|private|protected|virtual|abstract|with\s+sharing|"
    r"without\s+sharing|inherited\s+sharing)?\s*"
    r"(?:class|interface|enum)\s+(\w+)",
    re.IGNORECASE | re.MULTILINE,
)
_APEX_TRIGGER_RE = re.compile(
    r"^\s*trigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE | re.MULTILINE,
)
_VF_CONTROLLER_RE = re.compile(
    r'\bcontroller\s*=\s*["\']([\w.]+)["\']',
    re.IGNORECASE,
)
_VF_EXTENSIONS_RE = re.compile(
    r'\bextensions\s*=\s*["\']([\w.,\s]+)["\']',
    re.IGNORECASE,
)
_AURA_CONTROLLER_RE = re.compile(
    r'<aura:component[^>]*\bcontroller\s*=\s*["\']([\w.]+)["\']',
    re.IGNORECASE,
)
_AURA_EXTENDS_RE = re.compile(
    r'\bextends\s*=\s*["\'](c:)?([\w]+)["\']',
    re.IGNORECASE,
)
_AURA_IMPLEMENTS_RE = re.compile(
    r'\bimplements\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LWC_IMPORT_RE = re.compile(
    r"""^\s*import\s+.+?\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_LWC_APEX_IMPORT_RE = re.compile(r"@salesforce/apex/(\w+)\.(\w+)")
_LWC_SCHEMA_IMPORT_RE = re.compile(r"@salesforce/schema/(\w+)\.(\w+)")
_LWC_C_TAG_RE = re.compile(r"<c-([\w-]+)")
_LWC_LIGHTNING_TAG_RE = re.compile(r"<lightning-([\w-]+)")

# Apex test markers — excluded from graph extraction (case-insensitive)
_APEX_TEST_ANNOTATION_NAMES: frozenset[str] = frozenset({"istest", "testsetup"})
_APEX_TEST_ANNOTATION_IN_TEXT_RE = re.compile(
    r"@\s*(?:isTest|IsTest|TestSetup)\b",
    re.IGNORECASE,
)


def is_salesforce_meta_xml(name: str) -> bool:
    """True for SFDX decomposed metadata sidecars (e.g. Account.object-meta.xml)."""
    lower = name.lower()
    return lower.endswith("-meta.xml") or bool(_META_XML_RE.search(name))


def is_lwc_js_meta_xml(name: str) -> bool:
    """True for LWC bundle descriptors (e.g. myCmp.js-meta.xml)."""
    return name.lower().endswith(_LWC_JS_META_SUFFIX)


def in_salesforce_tree(path: Path) -> bool:
    """Heuristic: path lives under a typical Salesforce DX or MDAPI tree."""
    parts_lower = [p.lower() for p in path.parts]
    if "force-app" in parts_lower or "unpackaged" in parts_lower:
        return True
    if "main" in parts_lower:
        try:
            idx = parts_lower.index("main")
            if idx + 1 < len(parts_lower) and parts_lower[idx + 1] == "default":
                return True
        except ValueError:
            pass
    if "metadata" in parts_lower and any(d in parts_lower for d in _SF_CONTENT_DIRS):
        return True
    return any(d in parts_lower for d in _SF_CONTENT_DIRS)


def _in_lwc_or_aura(path: Path) -> bool:
    parts_lower = {p.lower() for p in path.parts}
    return "lwc" in parts_lower or "aura" in parts_lower


def _in_lwc(path: Path) -> bool:
    return "lwc" in {p.lower() for p in path.parts}


def lwc_bundle_name(path: Path) -> str:
    """Return the LWC bundle folder name (parent of bundle source files)."""
    return path.parent.name


def _lwc_child_bundle_name(kebab: str) -> str:
    """Map template tag c-my-widget → bundle folder myWidget."""
    parts = kebab.split("-")
    if not parts:
        return kebab
    return parts[0] + "".join(p.title() for p in parts[1:])


def classify_salesforce(path: Path) -> Any | None:
    """Return FileType when this path is Salesforce source; else None."""
    from graphify.detect import FileType

    name_lower = path.name.lower()
    ext = path.suffix.lower()

    if is_salesforce_meta_xml(path.name):
        return FileType.CODE

    if ext in SALESFORCE_CORE_CODE_EXTENSIONS:
        return FileType.CODE

    if name_lower in _SF_PROJECT_FILES:
        return FileType.DOCUMENT

    if not in_salesforce_tree(path):
        return None

    if ext in SALESFORCE_REGISTRY_EXTENSIONS:
        return FileType.CODE

    if ext == ".xml":
        return FileType.CODE

    if _in_lwc(path) and ext in (".html", ".css", ".svg", ".js"):
        return FileType.CODE

    if ext in (".html", ".css", ".svg") and "aura" in {p.lower() for p in path.parts}:
        return FileType.CODE

    # MDAPI folder bundles (objects/MyObj__c/MyObj__c.object)
    if ext in {".object", ".flow", ".profile", ".permissionset", ".layout", ".labels"}:
        return FileType.CODE

    return None


def salesforce_code_extensions() -> frozenset[str]:
    """Extensions to merge into global CODE_EXTENSIONS (core + registry)."""
    return SALESFORCE_CORE_CODE_EXTENSIONS | SALESFORCE_REGISTRY_EXTENSIONS


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _make_id(*parts: str) -> str:
    from graphify.extract import _make_id as make_id

    return make_id(*parts)


def _file_stem(path: Path) -> str:
    from graphify.extract import _file_stem as stem

    return stem(path)


def _metadata_component_label(path: Path) -> str:
    """Derive a human label from a *-meta.xml or sidecar filename."""
    name = path.name
    if is_lwc_js_meta_xml(name):
        return name[: -len(_LWC_JS_META_SUFFIX)]
    if is_salesforce_meta_xml(name):
        base = name.rsplit("-meta.xml", 1)[0]
        if "." in base:
            return base.rsplit(".", 1)[-1]
        return base
    return path.stem


def _add_reference(
    *,
    comp_nid: str,
    val: str,
    line: int,
    tag: str,
    add_node: Callable[..., None],
    add_edge: Callable[..., None],
    ref_sf_type: str | None = None,
) -> None:
    for part in re.split(r"[,;]\s*", val):
        part = part.strip()
        if not part or part in (".", "-"):
            continue
        label = part.split(".")[-1]
        ref_nid = _make_id(label)
        add_node(ref_nid, part, line, ref_sf_type)
        add_edge(comp_nid, ref_nid, "references", line, context=tag)


def extract_salesforce_metadata(path: Path) -> dict:
    """Extract nodes and reference edges from Salesforce metadata XML."""
    str_path = str(path)
    stem = _file_stem(path)
    label = _metadata_component_label(path)
    file_nid = _make_id(str_path)
    comp_nid = _make_id(stem, label)
    sf_type = _sf_type_from_path(path)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(nid: str, node_label: str, line: int = 1, node_sf_type: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            entry: dict[str, Any] = {
                "id": nid,
                "label": node_label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            }
            if node_sf_type:
                entry["sf_type"] = node_sf_type
            nodes.append(entry)

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int = 1,
        *,
        context: str | None = None,
    ) -> None:
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    add_node(file_nid, path.name, node_sf_type=sf_type)
    add_node(comp_nid, label, node_sf_type=sf_type)
    add_edge(file_nid, comp_nid, "contains")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(text)
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    # First pass: pick up display label (<label>) and flow sub-type
    # (<processType>) so we can annotate the component node before the main
    # reference-extraction loop.
    display_label: str | None = None
    process_type: str | None = None
    for elem in root.iter():
        etag = _local_tag(elem.tag).lower()
        if etag == "label" and display_label is None:
            val = (elem.text or "").strip()
            if val:
                display_label = val
        elif etag == "processtype" and process_type is None:
            val = (elem.text or "").strip()
            if val:
                process_type = val
        if display_label and process_type:
            break

    # Update the component node: use the XML display label when available
    # (e.g. "Account Update" instead of the filename-derived "flow"), and
    # store processType as sf_subtype for flows.
    for node in nodes:
        if node["id"] == comp_nid:
            if display_label:
                node["label"] = display_label
            if process_type:
                node["sf_subtype"] = process_type
            break

    # Flow actionCalls may list actionName before or after actionType
    pending_action_name: str | None = None
    pending_apex_action = False

    for elem in root.iter():
        tag = _local_tag(elem.tag).lower()
        line = getattr(elem, "sourceline", None) or 1

        if tag == "actionname":
            val = (elem.text or "").strip()
            if val and pending_apex_action:
                _add_reference(
                    comp_nid=comp_nid,
                    val=val,
                    line=line,
                    tag="apexclass",
                    add_node=add_node,
                    add_edge=add_edge,
                    ref_sf_type="apex_class",
                )
                pending_action_name = None
                pending_apex_action = False
            elif val:
                pending_action_name = val
            continue

        if tag == "actiontype":
            is_apex = (elem.text or "").strip().lower() == "apex"
            if is_apex and pending_action_name:
                _add_reference(
                    comp_nid=comp_nid,
                    val=pending_action_name,
                    line=line,
                    tag="apexclass",
                    add_node=add_node,
                    add_edge=add_edge,
                    ref_sf_type="apex_class",
                )
                pending_action_name = None
                pending_apex_action = False
            elif is_apex:
                pending_apex_action = True
            else:
                pending_action_name = None
                pending_apex_action = False
            continue

        if tag == "actioncalls":
            pending_action_name = None
            pending_apex_action = False

        for attr_name, attr_val in elem.attrib.items():
            attr_local = _local_tag(attr_name).lower()
            if attr_local in _REFERENCE_ATTRS and attr_val.strip():
                _add_reference(
                    comp_nid=comp_nid,
                    val=attr_val.strip(),
                    line=line,
                    tag=attr_local,
                    add_node=add_node,
                    add_edge=add_edge,
                    ref_sf_type=_TAG_TO_SF_TYPE.get(attr_local),
                )

        # <fullname> and <apiname> identify the component itself — skip them
        # as reference targets to avoid self-referential noise edges.

        if tag in _REFERENCE_TAGS:
            val = (elem.text or elem.get("value") or elem.get("name") or "").strip()
            if not val:
                for child in elem:
                    child_tag = _local_tag(child.tag).lower()
                    if child_tag in ("name", "stringvalue", "elementreference", "string"):
                        val = (child.text or "").strip()
                        if val:
                            break
            if val:
                _add_reference(
                    comp_nid=comp_nid,
                    val=val,
                    line=line,
                    tag=tag,
                    add_node=add_node,
                    add_edge=add_edge,
                    ref_sf_type=_TAG_TO_SF_TYPE.get(tag),
                )

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_lwc_js_meta(path: Path) -> dict:
    """Extract LWC bundle metadata (targets, objects, masterLabel) from *.js-meta.xml."""
    result = extract_salesforce_metadata(path)
    if result.get("error"):
        return result

    bundle = lwc_bundle_name(path)
    stem = _file_stem(path)
    comp_label = _metadata_component_label(path)
    comp_nid = _make_id(stem, comp_label)
    for node in result.get("nodes", []):
        if node["id"] == comp_nid:
            node["label"] = f"{bundle} (LWC)"
            node["sf_type"] = "lwc_bundle"
            break

    return result


def _apex_annotation_is_test(name: str) -> bool:
    return name.casefold() in _APEX_TEST_ANNOTATION_NAMES


def _apex_text_has_test_annotation(text: str) -> bool:
    return bool(_APEX_TEST_ANNOTATION_IN_TEXT_RE.search(text))


def _apex_find_class_body(node: Any) -> Any | None:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.children:
        if child.type in ("class_body", "interface_body"):
            return child
    return None


def _apex_collect_test_exclusions(path: Path) -> tuple[bool, set[str]]:
    """Return (skip_entire_file, node_ids_to_drop) for Apex test classes/methods."""
    from graphify.extract import _java_method_annotation_names, _read_text

    excluded: set[str] = set()
    stem = _file_stem(path)

    try:
        mod = importlib.import_module("tree_sitter_java")
        from tree_sitter import Language, Parser

        language = Language(mod.language())
        parser = Parser(language)
        source = path.read_bytes()
        root = parser.parse(source).root_node
    except Exception:
        return _apex_collect_test_exclusions_from_text(path)

    all_class_nids: set[str] = set()
    test_class_nids: set[str] = set()

    def walk(
        node: Any,
        parent_class_nid: str | None,
        inside_test_class: bool,
    ) -> None:
        t = node.type

        if t in ("class_declaration", "interface_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                for child in node.children:
                    if child.type == "identifier":
                        name_node = child
                        break
            if name_node is None:
                return
            class_name = _read_text(name_node, source)
            class_nid = _make_id(stem, class_name)
            all_class_nids.add(class_nid)
            annos = _java_method_annotation_names(node, source)
            is_test_class = any(_apex_annotation_is_test(a) for a in annos)
            if is_test_class:
                test_class_nids.add(class_nid)
                excluded.add(class_nid)
            body = _apex_find_class_body(node)
            if body is not None:
                for child in body.children:
                    walk(child, class_nid, is_test_class)
            return

        if t in ("method_declaration", "constructor_declaration") and parent_class_nid:
            name_node = node.child_by_field_name("name")
            func_name = _read_text(name_node, source) if name_node else None
            if not func_name:
                return
            method_nid = _make_id(parent_class_nid, func_name)
            if inside_test_class:
                excluded.add(method_nid)
                return
            annos = _java_method_annotation_names(node, source)
            if any(_apex_annotation_is_test(a) for a in annos):
                excluded.add(method_nid)
            return

        for child in node.children:
            walk(child, parent_class_nid, inside_test_class)

    walk(root, None, False)
    skip_file = bool(all_class_nids) and all_class_nids <= test_class_nids
    return skip_file, excluded


def _apex_collect_test_exclusions_from_text(path: Path) -> tuple[bool, set[str]]:
    """Regex fallback when tree-sitter-java is unavailable or fails."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, set()

    stem = _file_stem(path)
    all_class_names: list[str] = []
    test_class_names: list[str] = []

    for match in _APEX_CLASS_RE.finditer(text):
        name = match.group(1)
        all_class_names.append(name)
        prefix = text[max(0, match.start() - 800): match.start()]
        if _apex_text_has_test_annotation(prefix):
            test_class_names.append(name)

    excluded = {_make_id(stem, name) for name in test_class_names}
    skip_file = bool(all_class_names) and len(test_class_names) == len(all_class_names)
    return skip_file, excluded


def _filter_apex_graph(result: dict, excluded_ids: set[str]) -> dict:
    if not excluded_ids:
        return result
    nodes = [n for n in result.get("nodes", []) if n["id"] not in excluded_ids]
    kept = {n["id"] for n in nodes}
    edges = [
        e
        for e in result.get("edges", [])
        if e.get("source") in kept and e.get("target") in kept
    ]
    result["nodes"] = nodes
    result["edges"] = edges
    return result


def extract_apex(path: Path) -> dict:
    """Extract Apex classes/triggers (tree-sitter-java + Apex-specific regex).

    Skips @isTest classes and @isTest / @TestSetup methods so test code does not
    pollute the knowledge graph.
    """
    from graphify.extract import extract_java

    excluded: set[str] = set()
    if path.suffix.lower() != ".trigger":
        skip_file, excluded = _apex_collect_test_exclusions(path)
        if skip_file:
            return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}

    result = extract_java(path)
    if result.get("error"):
        return result

    if excluded:
        result = _filter_apex_graph(result, excluded)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    stem = _file_stem(path)
    file_nid = _make_id(str_path)
    nodes = list(result.get("nodes", []))
    edges = list(result.get("edges", []))
    seen = {n["id"] for n in nodes}

    def add_node(nid: str, node_label: str, line: int, sf_type: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            entry: dict[str, Any] = {
                "id": nid,
                "label": node_label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            }
            if sf_type:
                entry["sf_type"] = sf_type
            nodes.append(entry)

    def add_edge(src: str, tgt: str, relation: str, line: int, *, context: str | None = None) -> None:
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    if path.suffix.lower() == ".trigger":
        for match in _APEX_TRIGGER_RE.finditer(text):
            trig_name, sobject = match.group(1), match.group(2)
            events_raw = match.group(3)
            events = [e.strip() for e in events_raw.split(",") if e.strip()]
            events_str = ", ".join(events)
            line = text[: match.start()].count("\n") + 1
            trig_nid = _make_id(stem, trig_name)
            add_node(trig_nid, f"trigger {trig_name} on {sobject} ({events_str})", line, "apex_trigger")
            add_edge(file_nid, trig_nid, "contains", line)
            obj_nid = _make_id(sobject)
            add_node(obj_nid, sobject, line, "sobject")
            add_edge(trig_nid, obj_nid, "references", line, context="sobject")
            for event in events:
                add_edge(trig_nid, obj_nid, "fires_on", line, context=event.replace(" ", "_"))
    else:
        for match in _APEX_CLASS_RE.finditer(text):
            name = match.group(1)
            line = text[: match.start()].count("\n") + 1
            prefix = text[max(0, match.start() - 800): match.start()]
            if _apex_text_has_test_annotation(prefix):
                continue
            if not any(n.get("label") == name for n in nodes):
                nid = _make_id(stem, name)
                add_node(nid, name, line, "apex_class")
                add_edge(file_nid, nid, "contains", line)

    result["nodes"] = nodes
    result["edges"] = edges
    return result


def extract_visualforce(path: Path) -> dict:
    """Extract Visualforce pages/components and controller references."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    stem = _file_stem(path)
    file_nid = _make_id(str_path)
    page_nid = _make_id(stem, path.stem)
    page_sf_type = _sf_type_from_path(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(nid: str, node_label: str, line: int = 1, sf_type: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            entry: dict[str, Any] = {
                "id": nid,
                "label": node_label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            }
            if sf_type:
                entry["sf_type"] = sf_type
            nodes.append(entry)

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int = 1,
        *,
        context: str | None = None,
    ) -> None:
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    add_node(file_nid, path.name, sf_type=page_sf_type)
    add_node(page_nid, path.stem, sf_type=page_sf_type)
    add_edge(file_nid, page_nid, "contains")

    for match in _VF_CONTROLLER_RE.finditer(text):
        ctrl = match.group(1).split(".")[-1]
        line = text[: match.start()].count("\n") + 1
        ctrl_nid = _make_id(ctrl)
        add_node(ctrl_nid, ctrl, line, "apex_class")
        add_edge(page_nid, ctrl_nid, "references", line, context="controller")

    for match in _VF_EXTENSIONS_RE.finditer(text):
        line = text[: match.start()].count("\n") + 1
        for ext in match.group(1).split(","):
            ext = ext.strip()
            if ext:
                ext_nid = _make_id(ext.split(".")[-1])
                add_node(ext_nid, ext, line, "apex_class")
                add_edge(page_nid, ext_nid, "references", line, context="extension")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_aura(path: Path) -> dict:
    """Extract Aura bundles (.cmp, .app, .evt, .intf, …)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    stem = _file_stem(path)
    bundle_nid = _make_id(stem, path.stem)
    file_nid = _make_id(str_path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(nid: str, node_label: str, line: int = 1, sf_type: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            entry: dict[str, Any] = {
                "id": nid,
                "label": node_label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            }
            if sf_type:
                entry["sf_type"] = sf_type
            nodes.append(entry)

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int = 1,
        *,
        context: str | None = None,
    ) -> None:
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    add_node(file_nid, path.name, sf_type="aura_bundle")
    add_node(bundle_nid, path.stem, sf_type="aura_bundle")
    add_edge(file_nid, bundle_nid, "contains")

    for match in _AURA_CONTROLLER_RE.finditer(text):
        ctrl = match.group(1).split(".")[-1]
        line = text[: match.start()].count("\n") + 1
        ctrl_nid = _make_id(ctrl)
        add_node(ctrl_nid, ctrl, line, "apex_class")
        add_edge(bundle_nid, ctrl_nid, "references", line, context="controller")

    for match in _AURA_EXTENDS_RE.finditer(text):
        parent = match.group(2)
        line = text[: match.start()].count("\n") + 1
        parent_nid = _make_id(parent)
        add_node(parent_nid, parent, line, "aura_bundle")
        add_edge(bundle_nid, parent_nid, "references", line, context="extends")

    for match in _AURA_IMPLEMENTS_RE.finditer(text):
        line = text[: match.start()].count("\n") + 1
        for iface in match.group(1).replace(",", " ").split():
            iface = iface.strip()
            if iface:
                iface_nid = _make_id(iface.replace("c:", ""))
                add_node(iface_nid, iface, line)
                add_edge(bundle_nid, iface_nid, "references", line, context="implements")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_lwc_bundle_file(path: Path) -> dict:
    """Extract LWC bundle files: JS imports, HTML child components, CSS bundle linkage."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    stem = _file_stem(path)
    bundle_name = lwc_bundle_name(path)
    bundle_nid = _make_id(stem, bundle_name)
    file_nid = _make_id(str_path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(nid: str, node_label: str, line: int = 1, sf_type: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            entry: dict[str, Any] = {
                "id": nid,
                "label": node_label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            }
            if sf_type:
                entry["sf_type"] = sf_type
            nodes.append(entry)

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int = 1,
        *,
        context: str | None = None,
    ) -> None:
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    add_node(file_nid, path.name, sf_type="lwc_bundle")
    add_node(bundle_nid, bundle_name, sf_type="lwc_bundle")
    add_edge(file_nid, bundle_nid, "contains")

    ext = path.suffix.lower()

    if ext == ".js":
        for match in _LWC_IMPORT_RE.finditer(text):
            spec = match.group(1)
            line = text[: match.start()].count("\n") + 1
            if spec.startswith("c/"):
                child = _lwc_child_bundle_name(spec.split("/", 1)[1])
                tgt_nid = _make_id(child)
                add_node(tgt_nid, child, line, "lwc_bundle")
                add_edge(bundle_nid, tgt_nid, "imports", line, context="lwc")
            elif spec.startswith("@"):
                apex = _LWC_APEX_IMPORT_RE.search(spec)
                if apex:
                    cls_name = apex.group(1)
                    method_name = apex.group(2)
                    cls_nid = _make_id(cls_name)
                    add_node(cls_nid, cls_name, line, "apex_class")
                    add_edge(bundle_nid, cls_nid, "references", line, context="apexclass")
                    # Also emit a method-level reference so the specific
                    # entry point is represented in the graph.
                    method_ref = f"{cls_name}.{method_name}"
                    method_nid = _make_id(cls_name, method_name)
                    add_node(method_nid, method_ref, line, "apex_method")
                    add_edge(bundle_nid, method_nid, "references", line, context="apexmethod")
                schema = _LWC_SCHEMA_IMPORT_RE.search(spec)
                if schema:
                    obj_name = schema.group(1)
                    obj_nid = _make_id(obj_name)
                    add_node(obj_nid, obj_name, line, "sobject")
                    add_edge(bundle_nid, obj_nid, "references", line, context="object")
        # Note: _LWC_APEX_IMPORT_RE.finditer pass removed — already handled
        # inside the _LWC_IMPORT_RE loop above, which avoids duplicate edges.

    elif ext == ".html":
        for match in _LWC_C_TAG_RE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            child = _lwc_child_bundle_name(match.group(1))
            tgt_nid = _make_id(child)
            add_node(tgt_nid, child, line, "lwc_bundle")
            add_edge(bundle_nid, tgt_nid, "references", line, context="lwc")

        for match in _LWC_LIGHTNING_TAG_RE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            tag = f"lightning-{match.group(1)}"
            tgt_nid = _make_id(tag)
            add_node(tgt_nid, tag, line)
            add_edge(bundle_nid, tgt_nid, "references", line, context="lightning")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def salesforce_extractor_for(path: Path) -> Callable[[Path], dict] | None:
    """Return an extract function for this Salesforce path, or None."""
    name = path.name
    ext = path.suffix.lower()

    if _in_lwc(path) and is_lwc_js_meta_xml(name):
        return extract_lwc_js_meta

    if is_salesforce_meta_xml(name):
        return extract_salesforce_metadata

    if ext in (".cls", ".trigger"):
        return extract_apex

    if ext in (".page", ".component"):
        return extract_visualforce

    if ext in (".cmp", ".app", ".evt", ".intf", ".auradoc", ".design"):
        return extract_aura

    if _in_lwc(path) and ext in (".js", ".html", ".css", ".svg"):
        return extract_lwc_bundle_file

    if in_salesforce_tree(path):
        if ext == ".xml":
            return extract_salesforce_metadata
        if ext in SALESFORCE_REGISTRY_EXTENSIONS:
            return extract_salesforce_metadata

    return None

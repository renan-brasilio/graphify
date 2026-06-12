# Salesforce DX / SFDX repository support for graphify detect + extract pipelines.
#
# Coverage is driven by the Salesforce Metadata Registry (source-deploy-retrieve)
# plus path-aware rules for bundle layouts (lwc/, aura/, force-app/main/default/).
#
# Node identity: Salesforce component names are org-unique per metadata type, so
# every extractor here builds node IDs as _sf_id(sf_type, name).  A reference to
# AccountService from a Flow, a Visualforce page, an LWC import, or a trigger
# therefore lands on the same node as the class definition itself — no
# symbol-resolution pass is needed for cross-component edges to connect.
from __future__ import annotations

import json
import re
from functools import lru_cache
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

# Apex, Aura, Visualforce — always code inside a Salesforce project
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

_SF_PROJECT_FILES: frozenset[str] = frozenset({
    "sfdx-project.json",
    "project-scratch-def.json",
    "package.xml",
    "destructivechanges.xml",
    "destructivechangespost.xml",
    "destructivechangespre.xml",
})

# Metadata types excluded from the graph entirely.  Profiles, permission sets,
# and layouts are permission/arrangement matrices that reference thousands of
# fields and classes each — they glue every community together and drown the
# actual architecture in noise.
_EXCLUDED_SF_TYPES: frozenset[str] = frozenset({
    "profile", "permissionset", "permissionsetgroup", "mutingpermissionset",
    "layout",
})
_EXCLUDED_SF_SUFFIXES: frozenset[str] = frozenset(f".{t}" for t in _EXCLUDED_SF_TYPES)

# *-meta.xml sidecars of code artifacts carry no graph information beyond the
# code file they describe (apiVersion/status) — skipping them avoids one junk
# node pair per class/trigger/page in the corpus.  LWC's js-meta.xml is NOT a
# plain sidecar (targets/objects/masterLabel) and is extracted.
_CODE_SIDECAR_TYPES: frozenset[str] = frozenset({
    "cls", "trigger", "page", "component", "cmp", "evt", "intf",
    "auradoc", "design", "css", "svg", "html",
})

# Platform marker values (capability flags, base components) — config noise,
# not architecture.  Skipped wherever references are emitted.
_PLATFORM_REF_PREFIXES: tuple[str, ...] = (
    "lightning__", "lightning:", "lightning-", "force:", "flexipage:",
    "standard__", "standard:", "utility:", "aura:", "ui:", "clients:",
)

# XML element local-names that denote cross-metadata references.  Curated to
# tags whose values are component names of a known type; generic tags such as
# "field", "assignments", "content" or "template" produce unqualified or
# internal-element noise nodes and are deliberately absent.  "label" and
# "fullname" describe the component itself, never a reference.
_REFERENCE_TAGS: frozenset[str] = frozenset({
    "apexclass", "apexpage", "apexcomponent", "controller", "extension",
    "referenceto", "customobject", "object", "objects", "targetobject",
    "sobjecttype", "sobject",
    "flow", "flowname", "flowdefinition", "subflow",
    "lightningcomponent", "lwccomponent",
    "page", "customtab", "tab", "application", "flexipage",
    "report", "dashboard", "reporttype", "emailtemplate",
    "contentasset", "namedcredential", "connectedapp", "custommetadata",
    "globalvalueset", "valueset", "recordtype", "quickaction",
    "extends", "implements",
})

# XML attributes (local-name, lowercased) that hold metadata references
_REFERENCE_ATTRS: frozenset[str] = frozenset({
    "object", "sobject", "sobjecttype", "referenceto", "class",
    "controller", "extension", "page", "flow", "tab", "recordtype", "entity",
})

# Map from -meta.xml type suffix to a canonical sf_type string.
_SF_META_TYPE_MAP: dict[str, str] = {
    "flow": "flow",
    "flowdefinition": "flow",
    "object": "sobject",
    "field": "sobject_field",
    "apexclass": "apex_class",
    "apextrigger": "apex_trigger",
    "auradefinitionbundle": "aura_bundle",
    "lightningcomponentbundle": "lwc_bundle",
    "page": "visualforce_page",
    "component": "visualforce_component",
    "workflow": "workflow",
    "approvalprocess": "approval_process",
    "app": "application",
    "customtab": "tab",
    "tab": "tab",
    "compactlayout": "compact_layout",
    "listview": "list_view",
    "validationrule": "validation_rule",
    "recordtype": "record_type",
    "globalvalueset": "global_value_set",
    "standardvalueset": "value_set",
    "customlabels": "custom_labels",
    "labels": "custom_labels",
    "connectedapp": "connected_app",
    "namedcredential": "named_credential",
    "remotesitesetting": "remote_site",
    "custommetadata": "custom_metadata",
    "md": "custom_metadata",
    "report": "report",
    "dashboard": "dashboard",
    "emailtemplate": "email_template",
    "email": "email_template",
    "contentasset": "content_asset",
    "staticresource": "static_resource",
    "resource": "static_resource",
    "quickaction": "quick_action",
    "flexipage": "flexipage",
}

# Map from a _REFERENCE_TAGS / _REFERENCE_ATTRS key to the sf_type of targets.
_TAG_TO_SF_TYPE: dict[str, str] = {
    "apexclass": "apex_class",
    "controller": "apex_class",
    "extension": "apex_class",
    "class": "apex_class",
    "apexpage": "visualforce_page",
    "page": "visualforce_page",
    "apexcomponent": "visualforce_component",
    "referenceto": "sobject",
    "customobject": "sobject",
    "object": "sobject",
    "objects": "sobject",
    "targetobject": "sobject",
    "sobjecttype": "sobject",
    "sobject": "sobject",
    "entity": "sobject",
    "flow": "flow",
    "flowname": "flow",
    "flowdefinition": "flow",
    "subflow": "flow",
    "lightningcomponent": "lwc_bundle",
    "lwccomponent": "lwc_bundle",
    "customtab": "tab",
    "tab": "tab",
    "application": "application",
    "flexipage": "flexipage",
    "report": "report",
    "dashboard": "dashboard",
    "reporttype": "report_type",
    "emailtemplate": "email_template",
    "contentasset": "content_asset",
    "namedcredential": "named_credential",
    "connectedapp": "connected_app",
    "custommetadata": "custom_metadata",
    "globalvalueset": "global_value_set",
    "valueset": "value_set",
    "recordtype": "record_type",
    "quickaction": "quick_action",
}

# Apex platform builtins — never graph nodes.  Without this stoplist, String /
# List / Map / System / Test accumulate edges from every class in the org and
# become the highest-degree nodes in the visualization, drowning real
# architecture.  Casefolded for comparison.
APEX_BUILTIN_TYPES: frozenset[str] = frozenset({
    # primitives & collections
    "string", "integer", "long", "double", "decimal", "boolean", "date",
    "datetime", "time", "blob", "id", "object", "sobject", "list", "set",
    "map", "void", "iterator", "iterable", "comparable", "comparator",
    # system namespaces / static utility classes
    "system", "test", "assert", "database", "schema", "trigger", "math",
    "json", "jsonparser", "jsongenerator", "limits", "userinfo", "messaging",
    "label", "page", "component", "site", "network", "cache", "eventbus",
    "search", "approval", "auth", "dom", "url", "type", "pattern", "matcher",
    "crypto", "encodingutil", "pagereference", "apexpages", "version",
    "request", "quiddity", "formula", "security", "stack", "sites",
    # callouts & REST
    "http", "httprequest", "httpresponse", "restcontext", "restrequest",
    "restresponse", "continuation", "httpcalloutmock", "webservicemock",
    "staticresourcecalloutmock", "multistaticresourcecalloutmock",
    # describe & metadata reflection
    "sobjecttype", "describesobjectresult", "describefieldresult",
    "recordtypeinfo", "picklistentry", "fieldset", "sobjectfield",
    # async / context interfaces
    "queueable", "batchable", "schedulable", "queueablecontext",
    "batchablecontext", "schedulablecontext", "installcontext",
    "uninstallcontext", "finalizer", "finalizercontext",
    # DML results & misc
    "savepoint", "saveresult", "deleteresult", "upsertresult", "undeleteresult",
    "leadconvert", "leadconvertresult", "xmlstreamreader", "xmlstreamwriter",
    # exceptions
    "exception", "aurahandledexception", "dmlexception", "queryexception",
    "nullpointerexception", "listexception", "mathexception", "typeexception",
    "calloutexception", "jsonexception", "limitexception", "sobjectexception",
    "stringexception", "illegalargumentexception", "securityexception",
    "noaccessexception", "nodataaccessexception", "searchexception",
    "invalidparametervalueexception", "xmlexception", "visualforceexception",
    "asyncexception", "emailexception", "handledexception",
})

# Standard objects commonly constructed with `new X(...)` — used to type
# constructor targets as sobjects rather than Apex classes.  Custom objects
# are recognized by their __c/__e/__b/__mdt suffix instead.
_STANDARD_SOBJECTS: frozenset[str] = frozenset({
    "account", "contact", "lead", "opportunity", "case", "user", "task",
    "event", "campaign", "campaignmember", "asset", "contract", "order",
    "orderitem", "product2", "pricebook2", "pricebookentry", "quote",
    "quotelineitem", "opportunitylineitem", "opportunitycontactrole",
    "accountcontactrelation", "casecomment", "attachment", "note",
    "contentversion", "contentdocument", "contentdocumentlink", "emailmessage",
    "feeditem", "feedcomment", "group", "groupmember", "queuesobject",
    "recordtype", "topic", "topicassignment", "platformevent",
})
_CUSTOM_SOBJECT_SUFFIX_RE = re.compile(
    r"__(c|e|b|mdt|x|kav|share|history|feed)$", re.IGNORECASE
)


def _sobject_like(name: str) -> bool:
    return (
        name.casefold() in _STANDARD_SOBJECTS
        or bool(_CUSTOM_SOBJECT_SUFFIX_RE.search(name))
    )


# ── ID helpers ─────────────────────────────────────────────────────────────────

def _make_id(*parts: str) -> str:
    from graphify.extract import _make_id as make_id

    return make_id(*parts)


def _sf_id(sf_type: str, *name_parts: str) -> str:
    """Canonical node ID for a Salesforce component: org-unique per type."""
    return _make_id("sf", sf_type, *name_parts)


# ── Project / path detection ──────────────────────────────────────────────────

@lru_cache(maxsize=8192)
def _dir_has_sfdx_project(directory: Path) -> bool:
    try:
        if (directory / "sfdx-project.json").is_file():
            return True
    except OSError:
        return False
    parent = directory.parent
    if parent == directory:
        return False
    return _dir_has_sfdx_project(parent)


def in_salesforce_tree(path: Path) -> bool:
    """True when the path belongs to a Salesforce DX / MDAPI project.

    Requires a real project marker — a ``force-app``/``unpackaged`` path
    component or an ``sfdx-project.json`` ancestor — rather than generic
    directory names (``pages/``, ``components/``…), so non-Salesforce repos
    never route through Salesforce extraction.
    """
    parts_lower = {p.lower() for p in path.parts}
    if "force-app" in parts_lower or "unpackaged" in parts_lower:
        return True
    parent = path.parent
    if not parent.parts:
        parent = Path(".")
    return _dir_has_sfdx_project(parent)


def is_salesforce_meta_xml(name: str) -> bool:
    """True for SFDX decomposed metadata sidecars (e.g. Account.object-meta.xml)."""
    lower = name.lower()
    return lower.endswith("-meta.xml") or bool(_META_XML_RE.search(name))


def is_lwc_js_meta_xml(name: str) -> bool:
    """True for LWC bundle descriptors (e.g. myCmp.js-meta.xml)."""
    return name.lower().endswith(_LWC_JS_META_SUFFIX)


def _meta_xml_type(name: str) -> str | None:
    """Return the registry type suffix of a *-meta.xml filename, lowercased."""
    lower = name.lower()
    if not lower.endswith("-meta.xml"):
        return None
    base = lower[: -len("-meta.xml")]
    if "." not in base:
        return None
    return base.rsplit(".", 1)[1]


def _is_excluded_sf_file(path: Path) -> bool:
    """Profiles, permission sets, and layouts are excluded from the graph."""
    metatype = _meta_xml_type(path.name)
    if metatype is not None and metatype in _EXCLUDED_SF_TYPES:
        return True
    return path.suffix.lower() in _EXCLUDED_SF_SUFFIXES


def _is_code_sidecar(path: Path) -> bool:
    """True for *-meta.xml sidecars of code files (no graph value of their own)."""
    metatype = _meta_xml_type(path.name)
    if metatype is None:
        return False
    if metatype == "app":
        # aura/MyApp/MyApp.app-meta.xml is a sidecar; applications/My.app-meta.xml
        # is a CustomApplication definition.
        return "aura" in {p.lower() for p in path.parts}
    return metatype in _CODE_SIDECAR_TYPES


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

    if not in_salesforce_tree(path):
        return None

    name_lower = path.name.lower()
    ext = path.suffix.lower()

    if _is_excluded_sf_file(path) or _is_code_sidecar(path):
        return None

    if is_salesforce_meta_xml(path.name):
        return FileType.CODE

    if ext in SALESFORCE_CORE_CODE_EXTENSIONS:
        return FileType.CODE

    if name_lower in _SF_PROJECT_FILES:
        return FileType.DOCUMENT

    if ext in SALESFORCE_REGISTRY_EXTENSIONS:
        return FileType.CODE

    if ext == ".xml":
        return FileType.CODE

    if _in_lwc(path) and ext in (".html", ".css", ".svg", ".js"):
        return FileType.CODE

    if ext in (".html", ".css", ".svg") and "aura" in {p.lower() for p in path.parts}:
        return FileType.CODE

    # MDAPI folder bundles (objects/MyObj__c/MyObj__c.object)
    if ext in {".object", ".flow", ".labels"}:
        return FileType.CODE

    return None


def salesforce_code_extensions() -> frozenset[str]:
    """Extensions to merge into global CODE_EXTENSIONS (core + registry,
    minus excluded permission/layout types)."""
    return (
        SALESFORCE_CORE_CODE_EXTENSIONS | SALESFORCE_REGISTRY_EXTENSIONS
    ) - _EXCLUDED_SF_SUFFIXES


# ── Metadata XML extraction ───────────────────────────────────────────────────

def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _sf_type_from_path(path: Path) -> str:
    """Infer the Salesforce component type from a file path."""
    ext = path.suffix.lower()
    parts_lower = {p.lower() for p in path.parts}
    if ext == ".cls":
        return "apex_class"
    if ext == ".trigger":
        return "apex_trigger"
    if ext == ".page":
        return "visualforce_page"
    if ext == ".component":
        return "visualforce_component"
    if ext == ".app":
        return "aura_bundle" if "aura" in parts_lower else "application"
    if ext in (".cmp", ".evt", ".intf", ".auradoc", ".design"):
        return "aura_bundle"
    metatype = _meta_xml_type(path.name)
    if metatype is not None:
        return _SF_META_TYPE_MAP.get(metatype, metatype)
    if ext == ".object":
        return "sobject"
    if ext == ".flow":
        return "flow"
    if ext == ".labels":
        return "custom_labels"
    if _in_lwc(path):
        return "lwc_bundle"
    if "aura" in parts_lower:
        return "aura_bundle"
    return "metadata"


def _metadata_component_name(path: Path) -> str:
    """Derive the API name from a *-meta.xml or sidecar filename."""
    name = path.name
    if is_lwc_js_meta_xml(name):
        return name[: -len(_LWC_JS_META_SUFFIX)]
    if is_salesforce_meta_xml(name):
        base = name.rsplit("-meta.xml", 1)[0]
        if "." in base:
            return base.rsplit(".", 1)[0]
        return base
    return path.stem


def _sobject_scope(path: Path) -> str | None:
    """Return the owning object name for decomposed object children
    (objects/<Obj>/fields/Industry.field-meta.xml → "Obj")."""
    parts = path.parts
    lowers = [p.lower() for p in parts]
    if "objects" not in lowers:
        return None
    idx = lowers.index("objects")
    if idx + 1 >= len(parts) - 1:
        return None
    return parts[idx + 1]


def _node_entry(
    nid: str,
    label: str,
    str_path: str,
    line: int,
    sf_type: str | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": nid,
        "label": label,
        "file_type": "code",
        "source_file": str_path,
        "source_location": f"L{line}",
        # Salesforce names are org-unique per type, so the same ID emitted from
        # several files is an intentional cross-file link — the extraction
        # pipeline's collision-disambiguation pass must not split it.
        "id_scope": "global",
    }
    if sf_type:
        entry["sf_type"] = sf_type
    return entry


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
        # Boolean/numeric element text is a config flag (e.g. a tab's
        # <customObject>true</customObject>), not a component reference.
        if part.casefold() in ("true", "false") or part.isdigit():
            continue
        if part.casefold().startswith(_PLATFORM_REF_PREFIXES):
            continue
        # Strip the c: / namespace qualifier; keep dotted names intact for
        # object-scoped types (Account.Industry) where the qualifier matters.
        if part.lower().startswith("c:"):
            part = part[2:]
        if ref_sf_type in ("sobject_field", "record_type", "quick_action"):
            name = part
        else:
            name = part.split(".")[-1]
        if not name:
            continue
        ref_nid = _sf_id(ref_sf_type, name) if ref_sf_type else _make_id(name)
        add_node(ref_nid, name, line, ref_sf_type)
        add_edge(comp_nid, ref_nid, "references", line, context=tag)


def extract_salesforce_metadata(
    path: Path,
    *,
    comp_nid: str | None = None,
    comp_label: str | None = None,
    comp_sf_type: str | None = None,
) -> dict:
    """Extract nodes and reference edges from Salesforce metadata XML."""
    import xml.etree.ElementTree as ET

    str_path = str(path)
    sf_type = comp_sf_type or _sf_type_from_path(path)
    api_name = _metadata_component_name(path)
    scope = _sobject_scope(path)
    if scope and scope.casefold() != api_name.casefold():
        qualified = f"{scope}.{api_name}"
    else:
        qualified = api_name
        scope = None
    nid = comp_nid or _sf_id(sf_type, qualified)
    label = comp_label or qualified
    file_nid = _make_id(str_path)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    edge_seen: set[tuple] = set()

    def add_node(node_id: str, node_label: str, line: int = 1, node_sf_type: str | None = None) -> None:
        if node_id not in seen:
            seen.add(node_id)
            nodes.append(_node_entry(node_id, node_label, str_path, line, node_sf_type))

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int = 1,
        *,
        context: str | None = None,
    ) -> None:
        key = (src, tgt, relation, context)
        if key in edge_seen:
            return
        edge_seen.add(key)
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
    add_node(nid, label, node_sf_type=sf_type)
    add_edge(file_nid, nid, "contains")

    # Decomposed object children belong to their object.
    if scope:
        obj_nid = _sf_id("sobject", scope)
        add_node(obj_nid, scope, 1, "sobject")
        add_edge(obj_nid, nid, "contains", context="object_child")

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

    # Use the XML display label when available (e.g. "Account Update" instead
    # of "Account_Update").  Scope-qualified names (Account.Industry) keep the
    # qualification — an unqualified display label would be ambiguous.
    for node in nodes:
        if node["id"] == nid:
            if display_label and comp_label is None and not scope:
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
                    comp_nid=nid,
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
                    comp_nid=nid,
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
                    comp_nid=nid,
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
                    comp_nid=nid,
                    val=val,
                    line=line,
                    tag=tag,
                    add_node=add_node,
                    add_edge=add_edge,
                    ref_sf_type=_TAG_TO_SF_TYPE.get(tag),
                )

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_lwc_js_meta(path: Path) -> dict:
    """Extract LWC bundle metadata (targets, objects) from *.js-meta.xml.

    The component node uses the same canonical ID as the bundle's source
    files, so the descriptor enriches the one bundle node instead of creating
    a second hub.
    """
    bundle = lwc_bundle_name(path)
    return extract_salesforce_metadata(
        path,
        comp_nid=_sf_id("lwc_bundle", bundle),
        comp_label=bundle,
        comp_sf_type="lwc_bundle",
    )


# ── Apex extraction (pure regex — no tree-sitter dependency) ─────────────────

_APEX_ANNOTATION_PREFIX_RE = re.compile(r"^(?:@\w+(?:\s*\([^)]*\))?\s*)+")
_APEX_ANNOTATION_NAME_RE = re.compile(r"@(\w+)")
_APEX_TEST_ANNOTATIONS: frozenset[str] = frozenset({"istest", "testsetup"})

_APEX_TRIGGER_HEADER_RE = re.compile(
    r"^trigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_APEX_DECL_RE = re.compile(
    r"^(?:(?:public|private|protected|global)\s+)?"
    r"(?:(?:with|without|inherited)\s+sharing\s+)?"
    r"(?:(?:abstract|virtual)\s+)?"
    r"(class|interface|enum)\s+(\w+)"
    r"(?:\s+extends\s+([\w.<>,\s]+?))?"
    r"(?:\s+implements\s+([\w.<>,\s]+?))?"
    r"\s*\{?\s*$",
    re.IGNORECASE,
)
_APEX_METHOD_RE = re.compile(
    r"^(?:(?:public|private|protected|global|webservice|static|override|"
    r"virtual|abstract|final|testmethod)\s+)+"
    r"(?:[\w.<>,\[\]\s]+?\s+)?(\w+)\s*\(",
    re.IGNORECASE,
)
_APEX_STATIC_CALL_RE = re.compile(r"\b([A-Z]\w*)\s*\.\s*\w+\s*\(")
_APEX_NEW_RE = re.compile(r"\bnew\s+([A-Z]\w*)\s*\(")
_APEX_SOQL_FROM_RE = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)
_APEX_SOQL_BLOCK_RE = re.compile(r"\[\s*SELECT\b[^\]]*", re.IGNORECASE)


def _mask_apex_source(text: str) -> str:
    """Blank out comments and string literals (newlines preserved) so regex
    passes never match inside them."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "'":
            j = i + 1
            while j < n and text[j] != "'":
                if text[j] == "\\":
                    j += 1
                j += 1
            j = min(j + 1, n)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _clean_type_name(raw: str) -> str:
    """``myns.BaseHandler<Account>`` → ``BaseHandler``."""
    name = re.sub(r"<.*>", "", raw).strip()
    return name.split(".")[-1].strip()


def extract_apex(path: Path) -> dict:
    """Extract Apex classes, interfaces, enums, methods, triggers, and their
    cross-component relationships (static calls, constructors, SOQL).

    Pure regex over comment/string-masked source — no tree-sitter dependency.
    @isTest classes and @isTest/@TestSetup methods are excluded so test code
    does not pollute the knowledge graph.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    is_trigger = path.suffix.lower() == ".trigger"
    file_nid = _make_id(str_path)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    edge_seen: set[tuple] = set()

    def add_node(nid: str, label: str, line: int, sf_type: str | None = None,
                 sf_subtype: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            entry = _node_entry(nid, label, str_path, line, sf_type)
            if sf_subtype:
                entry["sf_subtype"] = sf_subtype
            nodes.append(entry)

    def add_edge(src: str, tgt: str, relation: str, line: int, *,
                 context: str | None = None) -> None:
        key = (src, tgt, relation, context)
        if key in edge_seen:
            return
        edge_seen.add(key)
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

    masked = _mask_apex_source(text)
    lines = masked.splitlines()

    # ── Structural pass: declarations, methods, scope tracking ────────────────
    depth = 0
    # innermost-last stack of {name, nid, entry_depth, is_test}
    stack: list[dict[str, Any]] = []
    pending_decl: dict[str, Any] | None = None
    pending_annotations: list[str] = []
    classes_found = 0
    test_classes_found = 0
    owner_nid_by_line: list[str | None] = []
    owner_name_by_line: list[str | None] = []

    def _current_owner() -> tuple[str | None, str | None]:
        if any(entry["is_test"] for entry in stack):
            return None, None
        if stack:
            return stack[-1]["nid"], stack[-1]["name"]
        return None, None

    for lineno, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()

        ann_match = _APEX_ANNOTATION_PREFIX_RE.match(stripped)
        if ann_match:
            for am in _APEX_ANNOTATION_NAME_RE.finditer(ann_match.group(0)):
                pending_annotations.append(am.group(1).lower())
            stripped = stripped[ann_match.end():].strip()

        consumed = False
        if stripped:
            if is_trigger:
                tm = _APEX_TRIGGER_HEADER_RE.match(stripped)
                if tm and pending_decl is None and not stack:
                    trig_name, sobject = tm.group(1), tm.group(2)
                    events = [e.strip() for e in tm.group(3).split(",") if e.strip()]
                    trig_nid = _sf_id("apex_trigger", trig_name)
                    label = f"trigger {trig_name} on {sobject} ({', '.join(events)})"
                    add_node(trig_nid, label, lineno, "apex_trigger")
                    add_edge(file_nid, trig_nid, "contains", lineno)
                    obj_nid = _sf_id("sobject", sobject)
                    add_node(obj_nid, sobject, lineno, "sobject")
                    for event in events:
                        add_edge(trig_nid, obj_nid, "fires_on", lineno,
                                 context=event.replace(" ", "_"))
                    pending_decl = {"name": trig_name, "nid": trig_nid, "is_test": False}
                    pending_annotations = []
                    consumed = True

            if not consumed:
                dm = _APEX_DECL_RE.match(stripped)
                if dm:
                    kind, name = dm.group(1).lower(), dm.group(2)
                    is_test = (
                        any(a in _APEX_TEST_ANNOTATIONS for a in pending_annotations)
                        or any(entry["is_test"] for entry in stack)
                    )
                    if kind == "class":
                        classes_found += 1
                        if is_test:
                            test_classes_found += 1
                    cls_nid = _sf_id("apex_class", name)
                    if not is_test:
                        add_node(cls_nid, name, lineno, "apex_class",
                                 sf_subtype=kind if kind != "class" else None)
                        add_edge(file_nid, cls_nid, "contains", lineno)
                        for group, relation in ((dm.group(3), "extends"),
                                                (dm.group(4), "implements")):
                            if not group:
                                continue
                            for raw_target in group.split(","):
                                target = _clean_type_name(raw_target)
                                if not target or target.casefold() in APEX_BUILTIN_TYPES:
                                    continue
                                tgt_nid = _sf_id("apex_class", target)
                                add_node(tgt_nid, target, lineno, "apex_class")
                                add_edge(cls_nid, tgt_nid, relation, lineno)
                    pending_decl = {"name": name, "nid": cls_nid, "is_test": is_test}
                    pending_annotations = []
                    consumed = True

            if not consumed and stack:
                owner_nid, owner_name = _current_owner()
                mm = _APEX_METHOD_RE.match(stripped)
                if mm:
                    method_name = mm.group(1)
                    method_is_test = any(
                        a in _APEX_TEST_ANNOTATIONS for a in pending_annotations
                    ) or "testmethod" in stripped.lower().split("(")[0]
                    if owner_nid is not None and not method_is_test:
                        method_nid = _sf_id("apex_class", owner_name, method_name)
                        sf_subtype = None
                        if "auraenabled" in pending_annotations:
                            sf_subtype = "aura_enabled"
                        elif "invocablemethod" in pending_annotations:
                            sf_subtype = "invocable"
                        add_node(method_nid, f"{owner_name}.{method_name}()",
                                 lineno, "apex_method", sf_subtype=sf_subtype)
                        add_edge(owner_nid, method_nid, "method", lineno)
                    pending_annotations = []
                    consumed = True

            if not consumed and not ann_match:
                pending_annotations = []

        owner_nid, owner_name = _current_owner()
        owner_nid_by_line.append(owner_nid)
        owner_name_by_line.append(owner_name)

        opens = raw_line.count("{")
        closes = raw_line.count("}")
        if pending_decl is not None and opens > 0:
            depth += 1
            stack.append({**pending_decl, "entry_depth": depth})
            pending_decl = None
            opens -= 1
        depth += opens - closes
        while stack and depth < stack[-1]["entry_depth"]:
            stack.pop()

    if not is_trigger and classes_found > 0 and classes_found == test_classes_found:
        return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}

    if nodes:
        add_node(file_nid, path.name, 1,
                 "apex_trigger" if is_trigger else "apex_class")

    # ── Relationship pass over the masked source ──────────────────────────────
    line_starts = [0]
    for offset, ch in enumerate(masked):
        if ch == "\n":
            line_starts.append(offset + 1)

    from bisect import bisect_right

    def _owner_at(pos: int) -> tuple[str | None, str | None]:
        idx = bisect_right(line_starts, pos) - 1
        if 0 <= idx < len(owner_nid_by_line):
            return owner_nid_by_line[idx], owner_name_by_line[idx]
        return None, None

    soql_spans: list[tuple[int, int]] = [
        m.span() for m in _APEX_SOQL_BLOCK_RE.finditer(masked)
    ]

    def _in_soql(pos: int) -> bool:
        return any(start <= pos < end for start, end in soql_spans)

    for m in _APEX_STATIC_CALL_RE.finditer(masked):
        owner_nid, owner_name = _owner_at(m.start())
        if owner_nid is None:
            continue
        target = m.group(1)
        if target == owner_name or target.casefold() in APEX_BUILTIN_TYPES:
            continue
        if _in_soql(m.start()):
            continue
        line = bisect_right(line_starts, m.start())
        tgt_nid = _sf_id("apex_class", target)
        add_node(tgt_nid, target, line, "apex_class")
        add_edge(owner_nid, tgt_nid, "calls", line, context="static_call")

    for m in _APEX_NEW_RE.finditer(masked):
        owner_nid, owner_name = _owner_at(m.start())
        if owner_nid is None:
            continue
        target = m.group(1)
        if target == owner_name or target.casefold() in APEX_BUILTIN_TYPES:
            continue
        line = bisect_right(line_starts, m.start())
        if _sobject_like(target):
            tgt_nid = _sf_id("sobject", target)
            add_node(tgt_nid, target, line, "sobject")
            add_edge(owner_nid, tgt_nid, "references", line, context="constructor")
        else:
            tgt_nid = _sf_id("apex_class", target)
            add_node(tgt_nid, target, line, "apex_class")
            add_edge(owner_nid, tgt_nid, "references", line, context="constructor")

    for block in _APEX_SOQL_BLOCK_RE.finditer(masked):
        owner_nid, _ = _owner_at(block.start())
        if owner_nid is None:
            continue
        for m in _APEX_SOQL_FROM_RE.finditer(block.group(0)):
            sobject = m.group(1)
            if sobject.casefold().endswith("__r"):
                continue  # child relationship traversal, not an object name
            line = bisect_right(line_starts, block.start() + m.start())
            obj_nid = _sf_id("sobject", sobject)
            add_node(obj_nid, sobject, line, "sobject")
            add_edge(owner_nid, obj_nid, "queries", line, context="soql")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


# ── Visualforce / Aura / LWC extraction ───────────────────────────────────────

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
_LWC_SCHEMA_IMPORT_RE = re.compile(r"@salesforce/schema/(\w+)(?:\.(\w+))?")
_LWC_C_TAG_RE = re.compile(r"<c-([\w-]+)")


def _component_extraction_helpers(str_path: str) -> tuple[list, list, Callable, Callable]:
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    edge_seen: set[tuple] = set()

    def add_node(nid: str, label: str, line: int = 1, sf_type: str | None = None) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append(_node_entry(nid, label, str_path, line, sf_type))

    def add_edge(src: str, tgt: str, relation: str, line: int = 1, *,
                 context: str | None = None) -> None:
        key = (src, tgt, relation, context)
        if key in edge_seen:
            return
        edge_seen.add(key)
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

    return nodes, edges, add_node, add_edge


def extract_visualforce(path: Path) -> dict:
    """Extract Visualforce pages/components and controller references."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    file_nid = _make_id(str_path)
    page_sf_type = _sf_type_from_path(path)
    page_nid = _sf_id(page_sf_type, path.stem)
    nodes, edges, add_node, add_edge = _component_extraction_helpers(str_path)

    add_node(file_nid, path.name, 1, page_sf_type)
    add_node(page_nid, path.stem, 1, page_sf_type)
    add_edge(file_nid, page_nid, "contains")

    for match in _VF_CONTROLLER_RE.finditer(text):
        ctrl = match.group(1).split(".")[-1]
        line = text[: match.start()].count("\n") + 1
        ctrl_nid = _sf_id("apex_class", ctrl)
        add_node(ctrl_nid, ctrl, line, "apex_class")
        add_edge(page_nid, ctrl_nid, "references", line, context="controller")

    for match in _VF_EXTENSIONS_RE.finditer(text):
        line = text[: match.start()].count("\n") + 1
        for ext in match.group(1).split(","):
            ext = ext.strip()
            if ext:
                ext_name = ext.split(".")[-1]
                ext_nid = _sf_id("apex_class", ext_name)
                add_node(ext_nid, ext_name, line, "apex_class")
                add_edge(page_nid, ext_nid, "references", line, context="extension")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_aura(path: Path) -> dict:
    """Extract Aura bundles (.cmp, .app, .evt, .intf, …)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    file_nid = _make_id(str_path)
    bundle_nid = _sf_id("aura_bundle", path.stem)
    nodes, edges, add_node, add_edge = _component_extraction_helpers(str_path)

    add_node(file_nid, path.name, 1, "aura_bundle")
    add_node(bundle_nid, path.stem, 1, "aura_bundle")
    add_edge(file_nid, bundle_nid, "contains")

    for match in _AURA_CONTROLLER_RE.finditer(text):
        ctrl = match.group(1).split(".")[-1]
        line = text[: match.start()].count("\n") + 1
        ctrl_nid = _sf_id("apex_class", ctrl)
        add_node(ctrl_nid, ctrl, line, "apex_class")
        add_edge(bundle_nid, ctrl_nid, "references", line, context="controller")

    for match in _AURA_EXTENDS_RE.finditer(text):
        parent = match.group(2)
        line = text[: match.start()].count("\n") + 1
        parent_nid = _sf_id("aura_bundle", parent)
        add_node(parent_nid, parent, line, "aura_bundle")
        add_edge(bundle_nid, parent_nid, "references", line, context="extends")

    for match in _AURA_IMPLEMENTS_RE.finditer(text):
        line = text[: match.start()].count("\n") + 1
        for iface in match.group(1).replace(",", " ").split():
            iface = iface.strip()
            if not iface or iface.casefold().startswith(_PLATFORM_REF_PREFIXES):
                continue
            iface_name = iface[2:] if iface.lower().startswith("c:") else iface
            if not iface_name:
                continue
            iface_nid = _sf_id("aura_bundle", iface_name)
            add_node(iface_nid, iface_name, line, "aura_bundle")
            add_edge(bundle_nid, iface_nid, "references", line, context="implements")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_lwc_bundle_file(path: Path) -> dict:
    """Extract LWC bundle files: JS imports, HTML child components, CSS bundle
    linkage.  All files of a bundle share one canonical bundle node."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    str_path = str(path)
    file_nid = _make_id(str_path)
    bundle_name = lwc_bundle_name(path)
    bundle_nid = _sf_id("lwc_bundle", bundle_name)
    nodes, edges, add_node, add_edge = _component_extraction_helpers(str_path)

    add_node(file_nid, path.name, 1, "lwc_bundle")
    add_node(bundle_nid, bundle_name, 1, "lwc_bundle")
    add_edge(file_nid, bundle_nid, "contains")

    ext = path.suffix.lower()

    if ext == ".js":
        for match in _LWC_IMPORT_RE.finditer(text):
            spec = match.group(1)
            line = text[: match.start()].count("\n") + 1
            if spec.startswith("c/"):
                child = _lwc_child_bundle_name(spec.split("/", 1)[1])
                tgt_nid = _sf_id("lwc_bundle", child)
                add_node(tgt_nid, child, line, "lwc_bundle")
                add_edge(bundle_nid, tgt_nid, "imports", line, context="lwc")
            elif spec.startswith("@"):
                apex = _LWC_APEX_IMPORT_RE.search(spec)
                if apex:
                    cls_name = apex.group(1)
                    method_name = apex.group(2)
                    cls_nid = _sf_id("apex_class", cls_name)
                    add_node(cls_nid, cls_name, line, "apex_class")
                    add_edge(bundle_nid, cls_nid, "references", line, context="apexclass")
                    # Method-level reference: same ID shape the Apex extractor
                    # uses for definitions, so the edge lands on the real node.
                    method_nid = _sf_id("apex_class", cls_name, method_name)
                    add_node(method_nid, f"{cls_name}.{method_name}()", line, "apex_method")
                    add_edge(bundle_nid, method_nid, "references", line, context="apexmethod")
                schema = _LWC_SCHEMA_IMPORT_RE.search(spec)
                if schema:
                    obj_name = schema.group(1)
                    field_name = schema.group(2)
                    obj_nid = _sf_id("sobject", obj_name)
                    add_node(obj_nid, obj_name, line, "sobject")
                    add_edge(bundle_nid, obj_nid, "references", line, context="object")
                    if field_name:
                        field_nid = _sf_id("sobject_field", f"{obj_name}.{field_name}")
                        add_node(field_nid, f"{obj_name}.{field_name}", line, "sobject_field")
                        add_edge(bundle_nid, field_nid, "references", line, context="schema")

    elif ext == ".html":
        for match in _LWC_C_TAG_RE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            child = _lwc_child_bundle_name(match.group(1))
            tgt_nid = _sf_id("lwc_bundle", child)
            add_node(tgt_nid, child, line, "lwc_bundle")
            add_edge(bundle_nid, tgt_nid, "references", line, context="lwc")
        # lightning-* base components are platform builtins — not graphed.

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def salesforce_extractor_for(path: Path) -> Callable[[Path], dict] | None:
    """Return an extract function for this Salesforce path, or None."""
    if not in_salesforce_tree(path):
        return None

    name = path.name
    ext = path.suffix.lower()

    if _is_excluded_sf_file(path) or _is_code_sidecar(path):
        return None

    if name.lower() in _SF_PROJECT_FILES:
        return None  # classified DOCUMENT; handled by the semantic pipeline

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

    if ext == ".xml":
        return extract_salesforce_metadata

    if ext in SALESFORCE_REGISTRY_EXTENSIONS:
        return extract_salesforce_metadata

    return None

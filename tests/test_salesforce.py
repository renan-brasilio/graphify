"""Salesforce SFDX repository detection and extraction."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.detect import CODE_EXTENSIONS, FileType, _is_noise_dir, classify_file, detect
from graphify.extract import _get_extractor
from graphify.salesforce import (
    APEX_BUILTIN_TYPES,
    SALESFORCE_CORE_CODE_EXTENSIONS,
    SALESFORCE_REGISTRY_EXTENSIONS,
    classify_salesforce,
    extract_apex,
    extract_aura,
    extract_lwc_bundle_file,
    extract_lwc_js_meta,
    extract_salesforce_metadata,
    extract_visualforce,
    in_salesforce_tree,
    is_lwc_js_meta_xml,
    is_salesforce_meta_xml,
    lwc_bundle_name,
    salesforce_code_extensions,
    salesforce_extractor_for,
)

SF_FIXTURES = Path(__file__).parent / "fixtures" / "salesforce"
SF_ROOT = SF_FIXTURES / "force-app" / "main" / "default"
LWC_CARD = SF_ROOT / "lwc" / "accountCard"


def _labels(result: dict) -> list[str]:
    return [n["label"] for n in result["nodes"]]


def _ids(result: dict) -> set[str]:
    return {n["id"] for n in result["nodes"]}


def _edges(result: dict, **kwargs: str) -> list[dict]:
    return [e for e in result["edges"] if all(e.get(k) == v for k, v in kwargs.items())]


def test_registry_data_loaded():
    assert len(SALESFORCE_REGISTRY_EXTENSIONS) > 400


def test_sfdx_dir_is_skipped_during_detect():
    assert _is_noise_dir(".sfdx")
    root = SF_FIXTURES.parent.parent  # tests/fixtures/salesforce
    sfdx_tools = root / ".sfdx" / "tools"
    sfdx_tools.mkdir(parents=True, exist_ok=True)
    noise_cls = sfdx_tools / "ScratchOrg.cls"
    noise_cls.write_text("public class ScratchOrg {}", encoding="utf-8")
    try:
        result = detect(root)
        code_files = result["files"]["code"]
        assert not any(".sfdx" in p for p in code_files)
    finally:
        noise_cls.unlink(missing_ok=True)
        for d in (sfdx_tools, sfdx_tools.parent):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()


def test_salesforce_code_extensions_include_core():
    merged = salesforce_code_extensions()
    assert SALESFORCE_CORE_CODE_EXTENSIONS <= merged
    assert ".cls" in CODE_EXTENSIONS
    assert ".trigger" in CODE_EXTENSIONS
    assert ".cmp" in CODE_EXTENSIONS


def test_excluded_types_not_in_code_extensions():
    merged = salesforce_code_extensions()
    assert ".profile" not in merged
    assert ".permissionset" not in merged
    assert ".layout" not in merged


def test_is_salesforce_meta_xml():
    assert is_salesforce_meta_xml("Account.object-meta.xml")
    assert is_salesforce_meta_xml("MyFlow.flow-meta.xml")
    assert is_salesforce_meta_xml("accountCard.js-meta.xml")
    assert not is_salesforce_meta_xml("package.xml")


def test_is_lwc_js_meta_xml():
    assert is_lwc_js_meta_xml("accountCard.js-meta.xml")
    assert not is_lwc_js_meta_xml("Account.object-meta.xml")


def test_lwc_bundle_name():
    assert lwc_bundle_name(LWC_CARD / "accountCard.js") == "accountCard"


# ── SFDX project gating ───────────────────────────────────────────────────────

def test_in_salesforce_tree():
    assert in_salesforce_tree(SF_ROOT / "classes/AccountService.cls")
    assert not in_salesforce_tree(Path("src/lib/foo.py"))


def test_generic_dir_names_do_not_mark_salesforce(tmp_path: Path):
    # Next.js / React repos have pages/ and components/ dirs — they must not
    # route through Salesforce extraction without a real project marker.
    pages_xml = tmp_path / "myapp" / "pages" / "sitemap.xml"
    pages_xml.parent.mkdir(parents=True)
    pages_xml.write_text("<root/>", encoding="utf-8")
    assert not in_salesforce_tree(pages_xml)
    assert classify_salesforce(pages_xml) is None
    assert salesforce_extractor_for(pages_xml) is None


def test_lwc_dir_outside_sfdx_not_hijacked(tmp_path: Path):
    js = tmp_path / "frontend" / "lwc" / "widget" / "widget.js"
    js.parent.mkdir(parents=True)
    js.write_text("export const x = 1;", encoding="utf-8")
    assert salesforce_extractor_for(js) is None
    assert classify_salesforce(js) is None


def test_sfdx_project_marker_enables_routing(tmp_path: Path):
    root = tmp_path / "sfrepo"
    (root / "src" / "classes").mkdir(parents=True)
    (root / "sfdx-project.json").write_text("{}", encoding="utf-8")
    cls = root / "src" / "classes" / "Foo.cls"
    cls.write_text("public class Foo {}", encoding="utf-8")
    assert in_salesforce_tree(cls)
    assert classify_salesforce(cls) == FileType.CODE
    assert salesforce_extractor_for(cls) is extract_apex


# ── Classification ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "rel",
    [
        "classes/AccountService.cls",
        "triggers/AccountTrigger.trigger",
        "objects/Account/Account.object-meta.xml",
        "objects/Account/fields/Industry.field-meta.xml",
        "pages/AccountView.page",
        "aura/HelloWorld/HelloWorld.cmp",
        "lwc/accountCard/accountCard.js",
        "lwc/accountCard/accountCard.html",
        "lwc/accountCard/accountCard.css",
        "lwc/accountCard/accountCard.js-meta.xml",
        "lwc/utils/utils.js",
        "flows/Account_Update.flow-meta.xml",
    ],
)
def test_classify_salesforce_paths(rel: str):
    path = SF_ROOT / rel
    assert classify_salesforce(path) == FileType.CODE
    assert classify_file(path) == FileType.CODE


def test_code_sidecars_are_skipped():
    sidecar = SF_ROOT / "classes/AccountService.cls-meta.xml"
    assert classify_salesforce(sidecar) is None
    assert salesforce_extractor_for(sidecar) is None


def test_profiles_permsets_layouts_excluded():
    for name in (
        "profiles/Admin.profile-meta.xml",
        "permissionsets/Sales.permissionset-meta.xml",
        "layouts/Account-Account Layout.layout-meta.xml",
        "profiles/Admin.profile",  # MDAPI format
    ):
        path = SF_ROOT / name
        assert classify_salesforce(path) is None, name
        assert salesforce_extractor_for(path) is None, name


def test_detect_includes_full_lwc_bundle_and_flow():
    result = detect(SF_FIXTURES)
    code_files = result["files"]["code"]
    joined = "\n".join(code_files)
    assert "AccountService.cls" in joined
    assert "Account.object-meta.xml" in joined
    assert "accountCard.js" in joined
    assert "accountCard.html" in joined
    assert "accountCard.css" in joined
    assert "accountCard.js-meta.xml" in joined
    assert "Account_Update.flow-meta.xml" in joined


def test_extractor_dispatch():
    assert salesforce_extractor_for(SF_ROOT / "classes/AccountService.cls") is extract_apex
    assert salesforce_extractor_for(SF_ROOT / "pages/AccountView.page") is extract_visualforce
    assert salesforce_extractor_for(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp") is extract_aura
    assert salesforce_extractor_for(
        SF_ROOT / "objects/Account/fields/Industry.field-meta.xml"
    ) is extract_salesforce_metadata
    assert salesforce_extractor_for(LWC_CARD / "accountCard.js-meta.xml") is extract_lwc_js_meta
    assert salesforce_extractor_for(LWC_CARD / "accountCard.html") is extract_lwc_bundle_file
    assert _get_extractor(LWC_CARD / "accountCard.js-meta.xml") is extract_lwc_js_meta


# ── Apex extraction ───────────────────────────────────────────────────────────

def test_extract_apex_class_canonical_id():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    assert "error" not in result
    assert "sf_apex_class_accountservice" in _ids(result)
    assert "AccountService" in _labels(result)


def test_extract_apex_methods():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    assert "sf_apex_class_accountservice_getaccounts" in _ids(result)
    assert "sf_apex_class_accountservice_refresh" in _ids(result)
    method_edges = _edges(result, relation="method")
    assert len(method_edges) == 2


def test_extract_apex_cross_class_call():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    calls = _edges(result, relation="calls", context="static_call")
    assert any(e["target"] == "sf_apex_class_accountselector" for e in calls)


def test_extract_apex_soql_queries_edge():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    queries = _edges(result, relation="queries", context="soql")
    assert any(e["target"] == "sf_sobject_account" for e in queries)


def test_extract_apex_no_builtin_noise_nodes():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    for label in _labels(result):
        assert label.casefold() not in APEX_BUILTIN_TYPES, (
            f"platform builtin {label!r} should not become a graph node"
        )
    assert "String" not in _labels(result)
    assert "List" not in _labels(result)


def test_extract_apex_aura_enabled_subtype():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    method = next(
        n for n in result["nodes"] if n["id"] == "sf_apex_class_accountservice_getaccounts"
    )
    assert method.get("sf_subtype") == "aura_enabled"


def test_extract_apex_skips_test_class_and_methods():
    result = extract_apex(SF_ROOT / "classes/AccountServiceTest.cls")
    assert "error" not in result
    assert result["nodes"] == []
    assert result["edges"] == []


def test_extract_apex_keeps_production_when_test_class_present():
    prod = extract_apex(SF_ROOT / "classes/AccountService.cls")
    assert any("AccountService" in label for label in _labels(prod))
    assert any("refresh" in label for label in _labels(prod))


def test_extract_apex_test_methods_in_production_class(tmp_path: Path):
    cls = tmp_path / "classes" / "Mixed.cls"
    cls.parent.mkdir(parents=True)
    cls.write_text(
        "public class Mixed {\n"
        "    public static void run() {}\n"
        "    @isTest\n"
        "    static void testRun() {}\n"
        "}\n",
        encoding="utf-8",
    )
    result = extract_apex(cls)
    labels = _labels(result)
    assert any("run" in lbl for lbl in labels if "test" not in lbl.lower())
    assert not any("testRun" in lbl for lbl in labels)


# ── Trigger extraction ────────────────────────────────────────────────────────

def test_extract_apex_trigger_canonical_id_and_label():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    assert "sf_apex_trigger_accounttrigger" in _ids(result)
    trig_labels = [lbl for lbl in _labels(result) if "AccountTrigger" in lbl]
    assert any("before insert" in lbl.lower() for lbl in trig_labels)


def test_trigger_fires_on_edges():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    fires_on = _edges(result, relation="fires_on")
    assert fires_on
    assert all(e["target"] == "sf_sobject_account" for e in fires_on)


def test_trigger_calls_handler_class():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    calls = _edges(result, relation="calls", context="static_call")
    assert any(e["target"] == "sf_apex_class_accountservice" for e in calls)


def test_trigger_sf_type():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    trig = next(n for n in result["nodes"] if n["id"] == "sf_apex_trigger_accounttrigger")
    assert trig["sf_type"] == "apex_trigger"


def test_trigger_no_keyword_noise_nodes():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    for noise in ("before", "after", "on"):
        assert noise not in _labels(result)


# ── Metadata extraction ───────────────────────────────────────────────────────

def test_extract_metadata_field_qualified():
    result = extract_salesforce_metadata(
        SF_ROOT / "objects/Account/fields/Industry.field-meta.xml"
    )
    assert "sf_sobject_field_account_industry" in _ids(result)
    assert "Account.Industry" in _labels(result)
    contains = _edges(result, relation="contains", context="object_child")
    assert contains
    assert contains[0]["source"] == "sf_sobject_account"


def test_extract_metadata_object_uses_display_label():
    result = extract_salesforce_metadata(
        SF_ROOT / "objects/Account/Account.object-meta.xml"
    )
    assert "sf_sobject_account" in _ids(result)
    obj = next(n for n in result["nodes"] if n["id"] == "sf_sobject_account")
    assert obj["label"] == "Account"
    assert obj["sf_type"] == "sobject"


def test_extract_flow_references_apex_and_object():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    apex_refs = _edges(result, relation="references", context="apexclass")
    assert any(e["target"] == "sf_apex_class_accountservice" for e in apex_refs)
    obj_refs = _edges(result, relation="references", context="object")
    assert any(e["target"] == "sf_sobject_account" for e in obj_refs)


def test_flow_uses_xml_label_as_node_label():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    flow = next(n for n in result["nodes"] if n["id"] == "sf_flow_account_update")
    assert flow["label"] == "Account Update"


def test_flow_stores_process_type():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    flow = next(n for n in result["nodes"] if n["id"] == "sf_flow_account_update")
    assert flow.get("sf_subtype") == "AutoLaunchedFlow"


def test_flow_no_self_label_reference():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    refs = _edges(result, relation="references", context="label")
    assert not refs, "label elements should not produce reference edges"


def test_object_no_fullname_reference():
    result = extract_salesforce_metadata(
        SF_ROOT / "objects/Account/fields/Industry.field-meta.xml"
    )
    fullname_refs = [e for e in result["edges"] if e.get("context") == "fullname"]
    assert not fullname_refs, "fullname elements should not produce reference edges"


# ── Visualforce / Aura ────────────────────────────────────────────────────────

def test_extract_visualforce_controller():
    result = extract_visualforce(SF_ROOT / "pages/AccountView.page")
    ctrl_edges = _edges(result, relation="references", context="controller")
    assert any(e["target"] == "sf_apex_class_accountservice" for e in ctrl_edges)
    page = next(n for n in result["nodes"] if n["id"] == "sf_visualforce_page_accountview")
    assert page["sf_type"] == "visualforce_page"


def test_extract_aura_controller_and_extends():
    result = extract_aura(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp")
    ctrl_edges = _edges(result, relation="references", context="controller")
    assert any(e["target"] == "sf_apex_class_accountservice" for e in ctrl_edges)
    extends = _edges(result, relation="references", context="extends")
    assert any(e["target"] == "sf_aura_bundle_basecomponent" for e in extends)


def test_aura_platform_interfaces_dropped():
    result = extract_aura(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp")
    assert not any("flexipage" in lbl.lower() for lbl in _labels(result))


# ── LWC extraction ────────────────────────────────────────────────────────────

def test_extract_lwc_js_imports_and_apex():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    assert _edges(result, relation="imports", context="lwc")
    apex_edges = _edges(result, relation="references", context="apexclass")
    assert any(e["target"] == "sf_apex_class_accountservice" for e in apex_edges)


def test_lwc_apex_import_emits_method_reference():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    method_edges = _edges(result, relation="references", context="apexmethod")
    assert any(
        e["target"] == "sf_apex_class_accountservice_refresh" for e in method_edges
    ), "method reference must land on the Apex extractor's method node ID"


def test_extract_lwc_html_child_components():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.html")
    refs = _edges(result, relation="references", context="lwc")
    targets = {e["target"] for e in refs}
    assert "sf_lwc_bundle_utils" in targets
    assert "sf_lwc_bundle_detailpanel" in targets


def test_lwc_lightning_base_components_dropped():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.html")
    assert not any(lbl.startswith("lightning-") for lbl in _labels(result))


def test_extract_lwc_css_bundle_nodes():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.css")
    assert "sf_lwc_bundle_accountcard" in _ids(result)


def test_extract_lwc_js_meta_shares_bundle_node():
    result = extract_lwc_js_meta(LWC_CARD / "accountCard.js-meta.xml")
    assert "error" not in result
    assert "sf_lwc_bundle_accountcard" in _ids(result)
    assert "Account" in _labels(result)
    assert "Contact" in _labels(result)
    # Platform targets (lightning__RecordPage etc.) are config, not architecture
    assert not any("lightning__" in lbl for lbl in _labels(result))


def test_extract_lwc_sibling_bundle():
    result = extract_lwc_bundle_file(SF_ROOT / "lwc/utils/utils.js")
    assert "sf_lwc_bundle_utils" in _ids(result)


def test_lwc_apex_import_no_duplicate_edges():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    apex_class_edges = _edges(result, relation="references", context="apexclass")
    sources_targets = [(e["source"], e["target"]) for e in apex_class_edges]
    assert len(sources_targets) == len(set(sources_targets))


# ── sf_type annotations ───────────────────────────────────────────────────────

def _sf_types(result: dict) -> set[str]:
    return {n["sf_type"] for n in result["nodes"] if "sf_type" in n}


def test_sf_type_on_flow_nodes():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    assert "flow" in _sf_types(result)


def test_sf_type_on_apex_references_from_flow():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    apex_nodes = [n for n in result["nodes"] if n.get("sf_type") == "apex_class"]
    assert apex_nodes


def test_sf_type_on_lwc_bundle():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    assert "lwc_bundle" in _sf_types(result)


def test_sf_type_on_aura_bundle():
    result = extract_aura(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp")
    assert "aura_bundle" in _sf_types(result)


# ── Cross-file connectivity (the property that makes the graph useful) ───────

def test_cross_file_references_land_on_definition_nodes():
    """Component names are org-unique per type, so a reference from any file
    must produce the same node ID as the component's own definition."""
    apex = extract_apex(SF_ROOT / "classes/AccountService.cls")
    cls_def_ids = _ids(apex)
    assert "sf_apex_class_accountservice" in cls_def_ids
    assert "sf_apex_class_accountservice_refresh" in cls_def_ids

    obj = extract_salesforce_metadata(SF_ROOT / "objects/Account/Account.object-meta.xml")
    assert "sf_sobject_account" in _ids(obj)

    # Flow → Apex and Flow → SObject
    flow = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    flow_targets = {e["target"] for e in flow["edges"]}
    assert "sf_apex_class_accountservice" in flow_targets
    assert "sf_sobject_account" in flow_targets

    # Visualforce → Apex
    page = extract_visualforce(SF_ROOT / "pages/AccountView.page")
    assert "sf_apex_class_accountservice" in {e["target"] for e in page["edges"]}

    # Aura → Apex
    aura = extract_aura(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp")
    assert "sf_apex_class_accountservice" in {e["target"] for e in aura["edges"]}

    # LWC → Apex class, Apex method, and SObject
    lwc = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    lwc_targets = {e["target"] for e in lwc["edges"]}
    assert "sf_apex_class_accountservice" in lwc_targets
    assert "sf_apex_class_accountservice_refresh" in lwc_targets
    assert "sf_sobject_account" in lwc_targets

    # Trigger → Apex and Trigger → SObject
    trig = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    trig_targets = {e["target"] for e in trig["edges"]}
    assert "sf_apex_class_accountservice" in trig_targets
    assert "sf_sobject_account" in trig_targets

    # SOQL inside Apex → same SObject node as the object definition
    assert "sf_sobject_account" in {e["target"] for e in apex["edges"]}


def test_pipeline_does_not_split_global_ids(tmp_path: Path):
    """The extraction pipeline's collision-disambiguation pass must respect
    id_scope=global: one canonical node per component across the whole corpus,
    not per-file ghosts."""
    from graphify.extract import extract

    paths = sorted(
        p for p in SF_FIXTURES.rglob("*")
        if p.is_file() and p.name != "sfdx-project.json"
    )
    result = extract(paths, tmp_path, parallel=False)
    ids = [n["id"] for n in result["nodes"]]
    assert "sf_apex_class_accountservice" in ids
    assert not any(
        nid.endswith("_sf_apex_class_accountservice")
        for nid in ids
    ), "canonical Salesforce IDs must not be split into per-file ghosts"


def test_lwc_bundle_single_node_across_files():
    """js, html, css, and js-meta.xml of one bundle share one component node."""
    bundle_ids = set()
    for fname in ("accountCard.js", "accountCard.html", "accountCard.css"):
        result = extract_lwc_bundle_file(LWC_CARD / fname)
        bundle_ids |= {
            n["id"] for n in result["nodes"]
            if n.get("sf_type") == "lwc_bundle" and n["label"] == "accountCard"
        }
    meta = extract_lwc_js_meta(LWC_CARD / "accountCard.js-meta.xml")
    bundle_ids |= {
        n["id"] for n in meta["nodes"]
        if n.get("sf_type") == "lwc_bundle" and n["label"] == "accountCard"
    }
    assert bundle_ids == {"sf_lwc_bundle_accountcard"}

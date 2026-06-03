"""Salesforce SFDX repository detection and extraction."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.detect import CODE_EXTENSIONS, FileType, _is_noise_dir, classify_file, detect
from graphify.extract import _get_extractor
from graphify.salesforce import (
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


def test_in_salesforce_tree():
    assert in_salesforce_tree(SF_ROOT / "classes/AccountService.cls")
    assert not in_salesforce_tree(Path("src/lib/foo.py"))


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


def test_extract_apex_class():
    result = extract_apex(SF_ROOT / "classes/AccountService.cls")
    assert "error" not in result
    assert any("AccountService" in label for label in _labels(result))


def test_extract_apex_skips_test_class_and_methods():
    result = extract_apex(SF_ROOT / "classes/AccountServiceTest.cls")
    assert "error" not in result
    assert result["nodes"] == []
    assert result["edges"] == []
    labels = _labels(result)
    assert not any("AccountServiceTest" in label for label in labels)
    assert not any("setupData" in label for label in labels)
    assert not any("testRefresh" in label for label in labels)


def test_extract_apex_keeps_production_when_test_class_present():
    prod = extract_apex(SF_ROOT / "classes/AccountService.cls")
    assert any("AccountService" in label for label in _labels(prod))
    assert any(".refresh" in label or "refresh" in label.lower() for label in _labels(prod))


def test_extract_apex_trigger_references_sobject():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    assert any("AccountTrigger" in label for label in _labels(result))
    assert _edges(result, context="sobject")


def test_extract_metadata_field():
    result = extract_salesforce_metadata(
        SF_ROOT / "objects/Account/fields/Industry.field-meta.xml"
    )
    assert any("Industry" in label for label in _labels(result))


def test_extract_flow_references_apex_and_object():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    refs = _edges(result, relation="references")
    contexts = {e.get("context") for e in refs}
    assert "apexclass" in contexts or any(
        "AccountService" in label for label in _labels(result)
    )
    assert "object" in contexts or any("Account" in label for label in _labels(result))


def test_extract_visualforce_controller():
    result = extract_visualforce(SF_ROOT / "pages/AccountView.page")
    assert _edges(result, context="controller")


def test_extract_aura_controller():
    result = extract_aura(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp")
    assert _edges(result, context="controller")


def test_extract_lwc_js_imports_and_apex():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    assert _edges(result, relation="imports", context="lwc")
    assert _edges(result, context="apexclass")
    assert any("AccountService" in label for label in _labels(result))
    assert any("utils" in label for label in _labels(result))


def test_extract_lwc_html_child_components():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.html")
    refs = _edges(result, relation="references")
    contexts = {e.get("context") for e in refs}
    assert "lwc" in contexts
    assert "lightning" in contexts
    assert any("utils" in label for label in _labels(result))
    assert any("detailPanel" in label for label in _labels(result))


def test_extract_lwc_css_bundle_nodes():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.css")
    assert any("accountCard" in label for label in _labels(result))
    assert len(result["edges"]) >= 1


def test_extract_lwc_js_meta_objects_and_label():
    result = extract_lwc_js_meta(LWC_CARD / "accountCard.js-meta.xml")
    assert "error" not in result
    assert any("accountCard (LWC)" in label for label in _labels(result))
    refs = _edges(result, relation="references")
    assert refs
    assert any("Account" in label for label in _labels(result))
    assert any("Contact" in label for label in _labels(result))


def test_extract_lwc_sibling_bundle():
    result = extract_lwc_bundle_file(SF_ROOT / "lwc/utils/utils.js")
    assert any("utils" in label for label in _labels(result))


def _sf_types(result: dict) -> dict[str, str]:
    """Return {node_label: sf_type} for nodes that carry an sf_type."""
    return {n["label"]: n["sf_type"] for n in result["nodes"] if "sf_type" in n}


# ── sf_type on nodes ──────────────────────────────────────────────────────────

def test_sf_type_on_flow_nodes():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    types = _sf_types(result)
    assert any(t == "flow" for t in types.values())


def test_sf_type_on_sobject_nodes():
    result = extract_salesforce_metadata(SF_ROOT / "objects/Account/Account.object-meta.xml")
    types = _sf_types(result)
    assert any(t == "sobject" for t in types.values())


def test_sf_type_on_apex_references_from_flow():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    apex_nodes = [n for n in result["nodes"] if n.get("sf_type") == "apex_class"]
    assert apex_nodes, "expected at least one apex_class reference node"


def test_sf_type_on_lwc_bundle():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    types = _sf_types(result)
    assert any(t == "lwc_bundle" for t in types.values())


def test_sf_type_on_aura_bundle():
    result = extract_aura(SF_ROOT / "aura/HelloWorld/HelloWorld.cmp")
    types = _sf_types(result)
    assert any(t == "aura_bundle" for t in types.values())


def test_sf_type_on_visualforce_page():
    result = extract_visualforce(SF_ROOT / "pages/AccountView.page")
    types = _sf_types(result)
    assert any(t == "visualforce_page" for t in types.values())


# ── trigger event capture ─────────────────────────────────────────────────────

def test_trigger_label_includes_event():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    trig_labels = [n["label"] for n in result["nodes"] if "AccountTrigger" in n["label"]]
    assert trig_labels, "trigger node not found"
    assert any("before insert" in lbl.lower() for lbl in trig_labels)


def test_trigger_fires_on_edges():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    fires_on = _edges(result, relation="fires_on")
    assert fires_on, "expected fires_on edges from trigger"


def test_trigger_sf_type():
    result = extract_apex(SF_ROOT / "triggers/AccountTrigger.trigger")
    # Match only the trigger component node (label starts with "trigger "),
    # not the filename node (label ends with ".trigger").
    trig_nodes = [
        n for n in result["nodes"]
        if n.get("label", "").lower().startswith("trigger accounttrigger")
    ]
    assert trig_nodes
    assert all(n.get("sf_type") == "apex_trigger" for n in trig_nodes)


# ── display label from XML ────────────────────────────────────────────────────

def test_flow_uses_xml_label_as_node_label():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    assert any("Account Update" in label for label in _labels(result)), (
        "expected flow comp node to use XML <label> as display name"
    )


def test_flow_stores_process_type():
    result = extract_salesforce_metadata(SF_ROOT / "flows/Account_Update.flow-meta.xml")
    flow_nodes = [n for n in result["nodes"] if n.get("sf_type") == "flow"]
    assert flow_nodes
    assert any(n.get("sf_subtype") == "AutoLaunchedFlow" for n in flow_nodes)


def test_metadata_field_uses_xml_label():
    result = extract_salesforce_metadata(
        SF_ROOT / "objects/Account/fields/Industry.field-meta.xml"
    )
    labels = _labels(result)
    assert any(label == "Industry" for label in labels), (
        "expected comp node label to be 'Industry' from XML <label>"
    )


# ── no spurious self-reference edges ─────────────────────────────────────────

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


# ── LWC Apex method-level import ──────────────────────────────────────────────

def test_lwc_apex_import_emits_method_reference():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    method_edges = _edges(result, context="apexmethod")
    assert method_edges, "expected apexmethod reference edge"
    method_labels = [n["label"] for n in result["nodes"] if n.get("sf_type") == "apex_method"]
    assert any("refresh" in lbl for lbl in method_labels), (
        "expected method node label to contain 'refresh'"
    )


def test_lwc_apex_import_no_duplicate_edges():
    result = extract_lwc_bundle_file(LWC_CARD / "accountCard.js")
    apex_class_edges = _edges(result, context="apexclass")
    sources_targets = [(e["source"], e["target"]) for e in apex_class_edges]
    assert len(sources_targets) == len(set(sources_targets)), (
        "duplicate apexclass reference edges found"
    )

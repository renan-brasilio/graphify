"""Salesforce SFDX repository detection and extraction."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.detect import CODE_EXTENSIONS, FileType, classify_file, detect
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

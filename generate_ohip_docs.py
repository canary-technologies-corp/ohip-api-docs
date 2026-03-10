#!/usr/bin/env python3
"""Generate a single-file HTML documentation browser for OHIP API specs."""

import argparse
import base64
import gzip
import json
import os
import re
from pathlib import Path

API_GROUPS = {
    "Reservations": ["rsv", "rsvasync", "rsvcfg"],
    "Blocks & Groups": ["blk", "blkasync", "blkcfg"],
    "Front Desk": ["fof", "fofcfg"],
    "Cashiering": ["csh", "cshasync"],
    "CRM / Profiles": ["crm", "crmasync", "crmcfg", "cms"],
    "Rates & Availability": ["rtp", "rtpasync", "par", "parasync"],
    "Inventory & Rooms": ["inv", "invasync", "rmcfg", "rmr", "rmrcfg"],
    "Housekeeping": ["hsk"],
    "Events & Catering": ["evm", "evmcfg"],
    "Activities": ["act", "actcfg"],
    "Leisure": ["lms"],
    "Accounts Receivable": ["ars"],
    "Configuration": ["entcfg", "intcfg", "expcfg", "repcfg", "medcfg"],
    "List of Values": ["lov"],
    "Integration": ["int", "dvm", "chl"],
    "Auth & Provisioning": ["oauth", "tokenexchange", "ops"],
    "Back Office": ["bof"],
    "Outbound": ["crmoutbound", "cshoutbound", "fofoutbound"],
}

API_TO_GROUP = {}
for group, apis in API_GROUPS.items():
    for api_id in apis:
        API_TO_GROUP[api_id] = group

METHOD_ORDER = {"get": 0, "post": 1, "put": 2, "patch": 3, "delete": 4, "head": 5, "options": 6}
METHOD_SHORT = {"get": "G", "post": "P", "put": "U", "patch": "A", "delete": "D", "head": "H", "options": "O"}


def compress_b64(data: str) -> str:
    return base64.b64encode(gzip.compress(data.encode("utf-8"), compresslevel=9)).decode("ascii")


def resolve_param_ref(ref: str, spec: dict) -> dict | None:
    if not ref.startswith("#/parameters/"):
        return None
    name = ref[len("#/parameters/"):]
    return spec.get("parameters", {}).get(name)


def resolve_response_ref(ref: str, spec: dict) -> dict | None:
    if not ref.startswith("#/responses/"):
        return None
    name = ref[len("#/responses/"):]
    return spec.get("responses", {}).get(name)


def extract_endpoint(path: str, method: str, op: dict, spec: dict) -> dict:
    params = []
    for p in op.get("parameters", []):
        if "$ref" in p:
            resolved = resolve_param_ref(p["$ref"], spec)
            if resolved:
                p = resolved
            else:
                continue
        param = {
            "name": p.get("name", ""),
            "in": p.get("in", ""),
            "required": p.get("required", False),
            "description": p.get("description", ""),
        }
        if "type" in p:
            param["type"] = p["type"]
            if "items" in p:
                param["items"] = p["items"]
            if "enum" in p:
                param["enum"] = p["enum"]
            if "format" in p:
                param["format"] = p["format"]
        if "schema" in p:
            param["schema"] = p["schema"]
        params.append(param)

    responses = {}
    for code, resp in op.get("responses", {}).items():
        if "$ref" in resp:
            resolved = resolve_response_ref(resp["$ref"], spec)
            if resolved:
                resp = resolved
            else:
                continue
        r = {"description": resp.get("description", "")}
        if "schema" in resp:
            r["schema"] = resp["schema"]
        responses[code] = r

    return {
        "method": method,
        "path": path,
        "summary": op.get("summary", ""),
        "operationId": op.get("operationId", ""),
        "description": op.get("description", ""),
        "tags": op.get("tags", []),
        "parameters": params,
        "responses": responses,
        "consumes": op.get("consumes", []),
        "produces": op.get("produces", []),
    }


def parse_rest_spec(filepath: Path) -> tuple[dict, dict]:
    with open(filepath) as f:
        spec = json.load(f)

    api_id = filepath.stem
    info = spec.get("info", {})
    title = info.get("title", api_id)
    description = info.get("description", "")
    version = info.get("version", "")
    base_path = spec.get("basePath", "")

    endpoints = []
    index_endpoints = []
    all_tags = set()

    for path, path_item in sorted(spec.get("paths", {}).items()):
        for method in sorted(path_item.keys(), key=lambda m: METHOD_ORDER.get(m, 99)):
            if method in ("get", "post", "put", "patch", "delete", "head", "options"):
                op = path_item[method]
                endpoint = extract_endpoint(path, method, op, spec)
                endpoints.append(endpoint)
                for t in op.get("tags", []):
                    all_tags.add(t)
                index_endpoints.append({
                    "m": METHOD_SHORT.get(method, "?"),
                    "p": path,
                    "s": op.get("summary", ""),
                    "o": op.get("operationId", ""),
                })

    definitions = spec.get("definitions", {})

    index_entry = {
        "id": api_id,
        "title": title,
        "desc": description[:300],
        "version": version,
        "basePath": base_path,
        "tags": sorted(all_tags),
        "group": API_TO_GROUP.get(api_id, "Other"),
        "endpointCount": len(endpoints),
        "endpoints": index_endpoints,
    }

    detail_blob = {
        "title": title,
        "description": description,
        "version": version,
        "basePath": base_path,
        "endpoints": endpoints,
        "definitions": definitions,
    }

    return index_entry, detail_blob


def parse_rest_specs(base_path: Path) -> tuple[list[dict], dict[str, str]]:
    search_index = []
    compressed_details = {}

    json_files = sorted(base_path.glob("*.json"))
    outbound_dir = base_path / "outbound"
    if outbound_dir.exists():
        json_files.extend(sorted(outbound_dir.glob("*.json")))

    for filepath in json_files:
        try:
            index_entry, detail_blob = parse_rest_spec(filepath)
            search_index.append(index_entry)
            compressed_details[index_entry["id"]] = compress_b64(json.dumps(detail_blob))
        except Exception as e:
            print(f"  Warning: Failed to parse {filepath.name}: {e}")

    print(f"  Parsed {len(search_index)} REST APIs, {sum(e['endpointCount'] for e in search_index)} endpoints")
    return search_index, compressed_details


def parse_graphql_schema(filepath: Path) -> tuple[dict, str]:
    sdl = filepath.read_text()
    schema_id = filepath.stem

    title = schema_id
    description = ""
    version = ""
    for line in sdl.split("\n")[:20]:
        if m := re.match(r"#\s*title:\s*(.+)", line, re.I):
            title = m.group(1).strip()
        elif m := re.match(r"#\s*description:\s*(.+)", line, re.I):
            description = m.group(1).strip()
        elif m := re.match(r"#\s*version:\s*(.+)", line, re.I):
            version = m.group(1).strip()

    type_count = len(re.findall(r"^\s*type\s+\w+", sdl, re.M))
    input_count = len(re.findall(r"^\s*input\s+\w+", sdl, re.M))
    enum_count = len(re.findall(r"^\s*enum\s+\w+", sdl, re.M))
    scalar_count = len(re.findall(r"^\s*scalar\s+\w+", sdl, re.M))

    query_fields = []
    query_match = re.search(r"type\s+Query\s*\{([^}]*)\}", sdl)
    if query_match:
        for fm in re.finditer(r"(\w+)\s*(?:\(|:)", query_match.group(1)):
            query_fields.append(fm.group(1))

    index_entry = {
        "id": schema_id,
        "title": title,
        "desc": description[:300],
        "version": version,
        "queryFields": query_fields,
        "typeCounts": {"type": type_count, "input": input_count, "enum": enum_count, "scalar": scalar_count},
    }

    return index_entry, compress_b64(sdl)


def parse_graphql_schemas(base_path: Path) -> tuple[list[dict], dict[str, str]]:
    search_index = []
    compressed_details = {}

    if not base_path.exists():
        print("  GraphQL data-apis directory not found, skipping")
        return search_index, compressed_details

    for filepath in sorted(base_path.glob("*.graphql")):
        try:
            index_entry, compressed_sdl = parse_graphql_schema(filepath)
            search_index.append(index_entry)
            compressed_details[index_entry["id"]] = compressed_sdl
        except Exception as e:
            print(f"  Warning: Failed to parse {filepath.name}: {e}")

    print(f"  Parsed {len(search_index)} GraphQL schemas")
    return search_index, compressed_details


def parse_streaming_schema(filepath: Path) -> tuple[dict | None, str | None]:
    if not filepath.exists():
        print("  Streaming schema not found, skipping")
        return None, None

    with open(filepath) as f:
        schema = json.load(f)

    types = []
    query_fields = []
    subscription_fields = []

    schema_data = schema.get("data", schema).get("__schema", schema.get("__schema", {}))
    for t in schema_data.get("types", []):
        name = t.get("name", "")
        if not name.startswith("__"):
            types.append(name)

    query_type_name = schema_data.get("queryType", {}).get("name", "Query")
    sub_type_name = schema_data.get("subscriptionType", {}).get("name", "Subscription")

    for t in schema_data.get("types", []):
        if t.get("name") == query_type_name:
            for f in t.get("fields", []):
                query_fields.append(f.get("name", ""))
        elif t.get("name") == sub_type_name:
            for f in t.get("fields", []):
                subscription_fields.append(f.get("name", ""))

    index_entry = {
        "title": "OHIP Streaming GraphQL API",
        "typeCount": len(types),
        "types": types,
        "queryFields": query_fields,
        "subscriptionFields": subscription_fields,
    }

    return index_entry, compress_b64(json.dumps(schema, indent=2))


def get_html_template():
    return r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OHIP API Documentation Browser</title>
<style>
:root {
  --bg: #ffffff;
  --bg-secondary: #f8f9fa;
  --bg-tertiary: #e9ecef;
  --text: #212529;
  --text-secondary: #6c757d;
  --border: #dee2e6;
  --accent: #0d6efd;
  --accent-hover: #0b5ed7;
  --sidebar-bg: #f8f9fa;
  --sidebar-width: 300px;
  --header-height: 56px;
  --get: #198754;
  --post: #0d6efd;
  --put: #fd7e14;
  --patch: #6f42c1;
  --delete: #dc3545;
  --get-bg: #d1e7dd;
  --post-bg: #cfe2ff;
  --put-bg: #ffe5d0;
  --patch-bg: #e2d9f3;
  --delete-bg: #f8d7da;
  --schema-bg: #f8f9fa;
  --highlight-bg: #fff3cd;
  --required-bg: #dc3545;
  --required-text: #fff;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: "SF Mono", "Fira Code", "Fira Mono", Menlo, Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1a1e;
    --bg-secondary: #222226;
    --bg-tertiary: #2e2e33;
    --text: #e8e8e8;
    --text-secondary: #a0a0a4;
    --border: #38383e;
    --accent: #5bb8ff;
    --accent-hover: #74c0fc;
    --sidebar-bg: #1e1e22;
    --get: #5cdb95;
    --post: #5bb8ff;
    --put: #ffb347;
    --patch: #c4a5ff;
    --delete: #ff7b7b;
    --get-bg: #1c2e22;
    --post-bg: #1c2430;
    --put-bg: #302818;
    --patch-bg: #28222e;
    --delete-bg: #2e1c1c;
    --schema-bg: #222226;
    --highlight-bg: #32301c;
  }
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--font);
  color: var(--text);
  background: var(--bg);
  line-height: 1.5;
  overflow: hidden;
  height: 100vh;
}

.header {
  position: fixed; top: 0; left: 0; right: 0;
  height: var(--header-height);
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center;
  padding: 0 16px; z-index: 100; gap: 16px;
}
.header h1 { font-size: 16px; font-weight: 600; white-space: nowrap; color: var(--text); }
.hamburger { display: none; background: none; border: none; font-size: 20px; cursor: pointer; color: var(--text); padding: 4px 8px; }
.search-wrapper { flex: 1; max-width: 600px; position: relative; }
.search-wrapper input {
  width: 100%; padding: 6px 12px 6px 32px;
  border: 1px solid var(--border); border-radius: 6px;
  font-size: 14px; background: var(--bg); color: var(--text); outline: none;
}
.search-wrapper input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(13,110,253,0.15); }
.search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--text-secondary); font-size: 14px; pointer-events: none; }
.search-scope {
  display: none; position: absolute; left: 32px; top: 50%; transform: translateY(-50%);
  background: var(--accent); color: #fff; font-size: 11px; padding: 1px 7px; border-radius: 3px;
  cursor: pointer; white-space: nowrap; z-index: 2; font-weight: 500; line-height: 18px;
}
.search-scope:hover { opacity: 0.85; }
.search-scope.active { display: inline-block; }
.search-wrapper input.scoped { padding-left: 120px; }
.search-results {
  position: fixed; top: var(--header-height); left: 50%; transform: translateX(-50%);
  width: min(700px, 90vw); max-height: min(500px, 70vh); overflow-y: auto;
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: 0 12px 40px rgba(0,0,0,0.4);
  display: none; z-index: 300; margin-top: 4px;
}
.search-results.active { display: block; }
.search-results.active ~ .search-overlay { display: block; }
.search-overlay {
  display: none; position: fixed; inset: 0; z-index: 250;
  background: rgba(0,0,0,0.3); backdrop-filter: blur(1px);
}
.search-result-item {
  padding: 10px 16px; cursor: pointer; border-bottom: 1px solid var(--border);
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 10px;
}
.search-result-item:hover, .search-result-item.selected { background: var(--bg-tertiary); }
.search-result-item .api-label { font-size: 11px; color: var(--text-secondary); min-width: 50px; text-transform: uppercase; font-weight: 600; letter-spacing: 0.3px; }
.search-result-item .path { font-family: var(--mono); font-size: 13px; word-break: break-all; }
.path .sep { color: var(--text-secondary); opacity: 0.5; }
.path .param { color: var(--accent); font-style: italic; }
.path .resource { color: var(--text); font-weight: 600; }
.path .segment { color: var(--text-secondary); }
.path .ellipsis { color: var(--text-secondary); opacity: 0.4; }
.path-wrap { position: relative; display: inline; }
.path-tip {
  display: none; position: absolute; left: 0; top: 100%; z-index: 50;
  background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 6px;
  padding: 8px 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.3);
  white-space: nowrap; font-family: var(--mono); font-size: 13px; margin-top: 4px;
  pointer-events: none;
}
.path-wrap:hover .path-tip { display: block; }
.endpoint-list-item .path { white-space: nowrap; }
.search-result-item .path-tip { position: fixed; left: auto; top: auto; }
.search-result-item .summary { font-size: 12px; color: var(--text-secondary); width: 100%; padding-left: 60px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stats { font-size: 12px; color: var(--text-secondary); white-space: nowrap; }

.layout { display: flex; height: calc(100vh - var(--header-height)); margin-top: var(--header-height); }

.sidebar {
  width: var(--sidebar-width); min-width: var(--sidebar-width);
  background: var(--sidebar-bg); border-right: 1px solid var(--border);
  overflow-y: auto; overflow-x: hidden; padding: 8px 0;
}
.sidebar-section-title { padding: 8px 16px 4px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary); }
.sidebar-group { margin-bottom: 2px; }
.sidebar-group-header { display: flex; align-items: center; padding: 4px 16px; cursor: pointer; font-size: 13px; font-weight: 600; color: var(--text); user-select: none; }
.sidebar-group-header:hover { background: var(--bg-tertiary); }
.sidebar-group-header .arrow { font-size: 10px; margin-right: 6px; transition: transform 0.15s; color: var(--text-secondary); }
.sidebar-group-header .arrow.open { transform: rotate(90deg); }
.sidebar-group-header .count { margin-left: auto; font-size: 11px; color: var(--text-secondary); font-weight: 400; }
.sidebar-group-items { display: none; }
.sidebar-group-items.open { display: block; }
.sidebar-api { padding: 3px 16px 3px 28px; font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 6px; color: var(--text); text-decoration: none; }
.sidebar-api:hover { background: var(--bg-tertiary); }
.sidebar-api.active { background: var(--accent); color: #fff; }
.sidebar-api .ep-count { margin-left: auto; font-size: 11px; color: var(--text-secondary); background: var(--bg-tertiary); padding: 0 6px; border-radius: 10px; }
.sidebar-api.active .ep-count { color: rgba(255,255,255,0.7); background: rgba(255,255,255,0.2); }
.sidebar-endpoints { display: none; }
.sidebar-endpoints.open { display: block; }
.sidebar-endpoint { padding: 2px 16px 2px 40px; font-size: 12px; cursor: pointer; display: flex; align-items: center; gap: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sidebar-endpoint:hover { background: var(--bg-tertiary); }
.sidebar-endpoint.active { background: var(--highlight-bg); }
.sidebar-endpoint .path { font-family: var(--mono); font-size: 11px; overflow: hidden; text-overflow: ellipsis; }

.method-badge { display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 3px; text-transform: uppercase; font-family: var(--mono); flex-shrink: 0; }
.method-badge.get { background: var(--get-bg); color: var(--get); }
.method-badge.post { background: var(--post-bg); color: var(--post); }
.method-badge.put { background: var(--put-bg); color: var(--put); }
.method-badge.patch { background: var(--patch-bg); color: var(--patch); }
.method-badge.delete { background: var(--delete-bg); color: var(--delete); }

.main { flex: 1; overflow-y: auto; padding: 24px 32px; }
.main h2 { font-size: 22px; margin-bottom: 8px; }
.main h3 { font-size: 16px; margin: 20px 0 8px; color: var(--text); }
.api-meta { font-size: 13px; color: var(--text-secondary); margin-bottom: 16px; }
.api-meta code { font-family: var(--mono); background: var(--bg-tertiary); padding: 2px 6px; border-radius: 3px; font-size: 12px; }
.api-desc { font-size: 14px; color: var(--text-secondary); margin-bottom: 20px; max-width: 800px; line-height: 1.6; }

.endpoint-list { list-style: none; }
.endpoint-list-item { padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 6px; cursor: pointer; display: flex; align-items: center; gap: 10px; transition: background 0.1s; }
.endpoint-list-item:hover { background: var(--bg-secondary); }
.endpoint-list-item .path { font-family: var(--mono); font-size: 13px; font-weight: 500; }
.endpoint-list-item .summary { font-size: 13px; color: var(--text-secondary); margin-left: auto; }

.endpoint-detail { max-width: 900px; }
.endpoint-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.endpoint-header .method-badge { font-size: 13px; padding: 3px 8px; }
.endpoint-header .path { font-family: var(--mono); font-size: 16px; font-weight: 600; word-break: break-all; }
.endpoint-summary { font-size: 15px; font-weight: 500; margin-bottom: 8px; }
.endpoint-desc { font-size: 14px; color: var(--text-secondary); margin-bottom: 16px; line-height: 1.6; }
.endpoint-operation-id { font-size: 12px; color: var(--text-secondary); margin-bottom: 16px; }
.endpoint-operation-id code { font-family: var(--mono); background: var(--bg-tertiary); padding: 2px 6px; border-radius: 3px; }
.breadcrumb { font-size: 13px; color: var(--text-secondary); margin-bottom: 16px; }
.breadcrumb a { color: var(--accent); text-decoration: none; cursor: pointer; }
.breadcrumb a:hover { text-decoration: underline; }

.params-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 20px; }
.params-table th { text-align: left; padding: 8px 10px; background: var(--bg-secondary); border-bottom: 2px solid var(--border); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-secondary); }
.params-table td { padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
.params-table tr:hover td { background: var(--bg-secondary); }
.params-table .param-name { font-family: var(--mono); font-weight: 500; white-space: nowrap; }
.params-table .param-in { font-size: 11px; padding: 1px 5px; border-radius: 3px; background: var(--bg-tertiary); color: var(--text-secondary); }
.required-badge { font-size: 10px; padding: 1px 5px; border-radius: 3px; background: var(--required-bg); color: var(--required-text); font-weight: 600; }
.param-type { font-family: var(--mono); font-size: 12px; color: var(--text-secondary); }
.param-enum { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }
.param-enum code { font-family: var(--mono); font-size: 11px; background: var(--bg-tertiary); padding: 1px 4px; border-radius: 2px; }

.schema-section { margin-top: 20px; }
.schema-tree { font-family: var(--mono); font-size: 13px; background: var(--schema-bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px 16px; overflow-x: auto; }
.schema-node { padding: 2px 0; }
.schema-key { color: var(--accent); }
.schema-type { color: var(--text-secondary); font-style: italic; }
.schema-desc { color: var(--text-secondary); font-size: 12px; font-family: var(--font); font-style: normal; }
.schema-toggle { cursor: pointer; user-select: none; color: var(--text-secondary); }
.schema-toggle:hover { color: var(--accent); }
.schema-ref { color: var(--accent); cursor: pointer; text-decoration: underline; text-decoration-style: dotted; }
.schema-ref:hover { text-decoration-style: solid; }
.schema-children { padding-left: 20px; border-left: 1px solid var(--border); margin-left: 4px; }

.response-code { display: inline-block; font-family: var(--mono); font-size: 12px; font-weight: 600; padding: 2px 6px; border-radius: 3px; margin-right: 8px; }
.response-code.c2xx { background: var(--get-bg); color: var(--get); }
.response-code.c4xx { background: var(--put-bg); color: var(--put); }
.response-code.c5xx { background: var(--delete-bg); color: var(--delete); }
.response-item { margin-bottom: 8px; }
.response-desc { font-size: 13px; color: var(--text-secondary); }

.graphql-sdl { font-family: var(--mono); font-size: 13px; background: var(--schema-bg); border: 1px solid var(--border); border-radius: 6px; padding: 16px; overflow-x: auto; white-space: pre; line-height: 1.5; max-height: 70vh; overflow-y: auto; }
.gql-keyword { color: #d73a49; font-weight: 600; }
.gql-type { color: #6f42c1; }
.gql-comment { color: var(--text-secondary); font-style: italic; }
.gql-string { color: #198754; }
.gql-directive { color: #fd7e14; }

@media (prefers-color-scheme: dark) {
  .gql-keyword { color: #ff8b82; }
  .gql-type { color: #c4a5ff; }
  .gql-string { color: #7ee787; }
  .gql-directive { color: #ffb86c; }
}

.welcome { max-width: 700px; margin: 40px auto; text-align: center; }
.welcome h2 { font-size: 28px; margin-bottom: 12px; }
.welcome p { font-size: 15px; color: var(--text-secondary); margin-bottom: 24px; line-height: 1.6; }
.welcome-stats { display: flex; gap: 24px; justify-content: center; flex-wrap: wrap; }
.stat-card { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 16px 24px; text-align: center; }
.stat-card .number { font-size: 28px; font-weight: 700; color: var(--accent); }
.stat-card .label { font-size: 13px; color: var(--text-secondary); }

.tag-pills { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 16px; }
.tag-pill { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--bg-tertiary); color: var(--text-secondary); cursor: pointer; }
.tag-pill:hover { background: var(--accent); color: #fff; }
.tag-pill.active { background: var(--accent); color: #fff; }

.copy-btn { background: none; border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer; color: var(--text-secondary); }
.copy-btn:hover { background: var(--bg-tertiary); }

.loading { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); padding: 20px; }
.spinner { width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 768px) {
  .hamburger { display: block; }
  .sidebar { position: fixed; top: var(--header-height); left: -320px; height: calc(100vh - var(--header-height)); z-index: 50; transition: left 0.2s; box-shadow: none; }
  .sidebar.open { left: 0; box-shadow: 4px 0 12px rgba(0,0,0,0.15); }
  .main { padding: 16px; }
  .overlay { display: none; position: fixed; inset: 0; top: var(--header-height); background: rgba(0,0,0,0.3); z-index: 40; }
  .overlay.active { display: block; }
}
</style>
</head>
<body>

<div class="header">
  <button class="hamburger" onclick="toggleSidebar()" aria-label="Toggle sidebar">&#9776;</button>
  <h1>OHIP API Docs</h1>
  <div class="search-wrapper">
    <span class="search-icon">&#128269;</span>
    <span class="search-scope" id="searchScope" onclick="toggleSearchScope()" title="Click to search all APIs"></span>
    <input type="text" id="searchInput" placeholder="Search endpoints... (press /)" autocomplete="off">
    <div class="search-results" id="searchResults"></div>
    <div class="search-overlay" id="searchOverlay" onclick="hideSearch()"></div>
  </div>
  <div class="stats" id="headerStats"></div>
</div>

<div class="overlay" id="overlay" onclick="toggleSidebar()"></div>

<div class="layout">
  <nav class="sidebar" id="sidebar"></nav>
  <main class="main" id="main"></main>
</div>

<script>
const SEARCH_INDEX = __SEARCH_INDEX__;
const REST_DETAIL = __REST_DETAIL__;
const GRAPHQL_INDEX = __GRAPHQL_INDEX__;
const GRAPHQL_DETAIL = __GRAPHQL_DETAIL__;
const STREAMING_INDEX = __STREAMING_INDEX__;
const STREAMING_DETAIL = __STREAMING_DETAIL__;
const API_GROUPS = __API_GROUPS__;

const detailCache = new Map();
let currentApiId = null;
let currentEndpointIdx = null;
let searchTimeout = null;
let searchSelectedIdx = -1;
let searchResults = [];
let activeTagFilter = null;
let searchScopedToApi = true;
let expandedGroups = new Set();
let expandedApis = new Set();

async function decompressBlob(b64) {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const ds = new DecompressionStream('gzip');
  const writer = ds.writable.getWriter();
  writer.write(bytes);
  writer.close();
  const reader = ds.readable.getReader();
  const chunks = [];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
  }
  const totalLen = chunks.reduce((s, c) => s + c.length, 0);
  const merged = new Uint8Array(totalLen);
  let offset = 0;
  for (const c of chunks) { merged.set(c, offset); offset += c.length; }
  return new TextDecoder().decode(merged);
}

async function getRestDetail(apiId) {
  const key = 'rest:' + apiId;
  if (detailCache.has(key)) return detailCache.get(key);
  const raw = await decompressBlob(REST_DETAIL[apiId]);
  const data = JSON.parse(raw);
  detailCache.set(key, data);
  return data;
}

async function getGraphqlDetail(schemaId) {
  const key = 'gql:' + schemaId;
  if (detailCache.has(key)) return detailCache.get(key);
  const sdl = await decompressBlob(GRAPHQL_DETAIL[schemaId]);
  detailCache.set(key, sdl);
  return sdl;
}

async function getStreamingDetail() {
  const key = 'streaming';
  if (detailCache.has(key)) return detailCache.get(key);
  if (!STREAMING_DETAIL) return null;
  const raw = await decompressBlob(STREAMING_DETAIL);
  const data = JSON.parse(raw);
  detailCache.set(key, data);
  return data;
}

function renderSidebar() {
  const sb = document.getElementById('sidebar');
  let html = '<div class="sidebar-section-title">REST APIs</div>';
  const groupOrder = Object.keys(API_GROUPS);
  for (const group of groupOrder) {
    const apiIds = API_GROUPS[group];
    const apis = SEARCH_INDEX.filter(a => apiIds.includes(a.id));
    if (apis.length === 0) continue;
    const isOpen = expandedGroups.has(group);
    const totalEndpoints = apis.reduce((s, a) => s + a.endpointCount, 0);
    html += '<div class="sidebar-group">' +
      '<div class="sidebar-group-header" onclick="toggleGroup(\'' + escAttr(group) + '\')">' +
      '<span class="arrow ' + (isOpen ? 'open' : '') + '">&#9654;</span>' +
      esc(group) +
      '<span class="count">' + totalEndpoints + '</span></div>' +
      '<div class="sidebar-group-items ' + (isOpen ? 'open' : '') + '">';
    for (const api of apis) {
      const isActive = currentApiId === api.id && currentEndpointIdx === null;
      const isExpanded = expandedApis.has(api.id);
      html += '<div class="sidebar-api ' + (isActive ? 'active' : '') + '" onclick="navigateApi(\'' + api.id + '\')">' +
        truncate(api.title.replace('OPERA Cloud ', '').replace(' API', ''), 30) +
        '<span class="ep-count">' + api.endpointCount + '</span></div>';
      if (isExpanded) {
        html += '<div class="sidebar-endpoints open">';
        for (let i = 0; i < api.endpoints.length; i++) {
          const ep = api.endpoints[i];
          const epActive = currentApiId === api.id && currentEndpointIdx === i;
          const mc = methodFromShort(ep.m);
          html += '<div class="sidebar-endpoint ' + (epActive ? 'active' : '') + '" onclick="event.stopPropagation(); navigateEndpoint(\'' + api.id + '\', ' + i + ')">' +
            '<span class="method-badge ' + mc + '">' + ep.m + '</span>' +
            '<span class="path">' + truncate(ep.p, 28) + '</span></div>';
        }
        html += '</div>';
      }
    }
    html += '</div></div>';
  }

  if (GRAPHQL_INDEX.length > 0) {
    const isOpen = expandedGroups.has('__graphql__');
    html += '<div class="sidebar-section-title" style="margin-top:12px">GraphQL Schemas</div>' +
      '<div class="sidebar-group">' +
      '<div class="sidebar-group-header" onclick="toggleGroup(\'__graphql__\')">' +
      '<span class="arrow ' + (isOpen ? 'open' : '') + '">&#9654;</span>Data APIs' +
      '<span class="count">' + GRAPHQL_INDEX.length + '</span></div>' +
      '<div class="sidebar-group-items ' + (isOpen ? 'open' : '') + '">';
    for (const schema of GRAPHQL_INDEX) {
      const isActive = currentApiId === 'gql:' + schema.id;
      html += '<div class="sidebar-api ' + (isActive ? 'active' : '') + '" onclick="navigateGraphql(\'' + escAttr(schema.id) + '\')">' +
        truncate(schema.id, 30) + '</div>';
    }
    html += '</div></div>';
    if (STREAMING_INDEX) {
      html += '<div class="sidebar-api ' + (currentApiId === 'streaming' ? 'active' : '') + '" onclick="navigateStreaming()" style="padding-left:16px; margin-top:4px">Streaming API</div>';
    }
  }
  sb.innerHTML = html;
}

function renderWelcome() {
  const totalEndpoints = SEARCH_INDEX.reduce((s, a) => s + a.endpointCount, 0);
  document.getElementById('main').innerHTML =
    '<div class="welcome"><h2>OHIP API Documentation</h2>' +
    '<p>Browse Oracle Hospitality Integration Platform APIs. Use the sidebar to navigate or press <kbd>/</kbd> to search.</p>' +
    '<div class="welcome-stats">' +
    '<div class="stat-card"><div class="number">' + SEARCH_INDEX.length + '</div><div class="label">REST APIs</div></div>' +
    '<div class="stat-card"><div class="number">' + totalEndpoints.toLocaleString() + '</div><div class="label">Endpoints</div></div>' +
    '<div class="stat-card"><div class="number">' + GRAPHQL_INDEX.length + '</div><div class="label">GraphQL Schemas</div></div>' +
    '</div></div>';
  document.getElementById('headerStats').textContent = SEARCH_INDEX.length + ' APIs \u00B7 ' + totalEndpoints.toLocaleString() + ' endpoints';
}

async function renderApiOverview(apiId) {
  const main = document.getElementById('main');
  const indexEntry = SEARCH_INDEX.find(a => a.id === apiId);
  if (!indexEntry) return;
  main.innerHTML = '<div class="loading"><div class="spinner"></div>Loading...</div>';
  const detail = await getRestDetail(apiId);

  var METHOD_RANK = {get: 0, post: 1, put: 2, patch: 3, delete: 4, head: 5, options: 6};
  function endpointSortKey(ep) {
    // Extract the resource path (strip param suffixes for grouping)
    var base = ep.path.replace(/\/{[^}]+}/g, '/_').replace(/\/action\/[^/]+$/, '');
    return base + '|' + (METHOD_RANK[ep.method] || 9);
  }
  let endpoints = detail.endpoints.slice();
  endpoints.sort(function(a, b) { return endpointSortKey(a).localeCompare(endpointSortKey(b)); });
  if (activeTagFilter) {
    endpoints = endpoints.filter(ep => ep.tags.includes(activeTagFilter));
  }

  const allTags = [...new Set(detail.endpoints.flatMap(ep => ep.tags))].sort();
  let tagsHtml = '';
  if (allTags.length > 1) {
    tagsHtml = '<div class="tag-pills">' +
      '<span class="tag-pill ' + (!activeTagFilter ? 'active' : '') + '" onclick="filterTag(null)">All</span>' +
      allTags.map(t => '<span class="tag-pill ' + (activeTagFilter === t ? 'active' : '') + '" onclick="filterTag(\'' + escAttr(t) + '\')">' + esc(t) + '</span>').join('') +
      '</div>';
  }

  main.innerHTML =
    '<h2>' + esc(detail.title) + '</h2>' +
    '<div class="api-meta">Base path: <code>' + esc(detail.basePath) + '</code> &middot; Version: ' + esc(detail.version) + ' &middot; ' + endpoints.length + ' endpoints</div>' +
    '<div class="api-desc">' + stripHtml(detail.description) + '</div>' +
    tagsHtml +
    '<ul class="endpoint-list">' + endpoints.map(function(ep) {
      var realIdx = detail.endpoints.indexOf(ep);
      return '<li class="endpoint-list-item" onclick="navigateEndpoint(\'' + apiId + '\', ' + realIdx + ')">' +
        '<span class="method-badge ' + ep.method + '">' + ep.method.toUpperCase() + '</span>' +
        pathWithTip(ep.path, 3) +
        '<span class="summary">' + esc(ep.summary) + '</span></li>';
    }).join('') + '</ul>';
}

async function renderEndpointDetail(apiId, idx) {
  const main = document.getElementById('main');
  main.innerHTML = '<div class="loading"><div class="spinner"></div>Loading...</div>';
  const detail = await getRestDetail(apiId);
  const ep = detail.endpoints[idx];
  if (!ep) return;

  const cleanDesc = stripHtml(ep.description);
  let paramsHtml = '';
  if (ep.parameters.length > 0) {
    paramsHtml = '<h3>Parameters</h3><table class="params-table"><thead><tr><th>Name</th><th>In</th><th>Required</th><th>Type</th><th>Description</th></tr></thead><tbody>' +
      ep.parameters.map(function(p) {
        var typeStr = formatParamType(p);
        var enumStr = p.enum ? '<div class="param-enum">' + p.enum.map(function(e) { return '<code>' + esc(String(e)) + '</code>'; }).join(' ') + '</div>' : '';
        return '<tr><td class="param-name">' + esc(p.name) + '</td>' +
          '<td><span class="param-in">' + esc(p["in"]) + '</span></td>' +
          '<td>' + (p.required ? '<span class="required-badge">required</span>' : '') + '</td>' +
          '<td><span class="param-type">' + esc(typeStr) + '</span>' + enumStr + '</td>' +
          '<td>' + esc(p.description) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  let responsesHtml = '<h3>Responses</h3>';
  for (const [code, resp] of Object.entries(ep.responses)) {
    const codeClass = code.startsWith('2') ? 'c2xx' : code.startsWith('4') ? 'c4xx' : code.startsWith('5') ? 'c5xx' : '';
    responsesHtml += '<div class="response-item"><span class="response-code ' + codeClass + '">' + esc(code) + '</span>' +
      '<span class="response-desc">' + esc(resp.description) + '</span>';
    if (resp.schema) {
      responsesHtml += '<div class="schema-section"><div class="schema-tree">' + renderSchema(resp.schema, detail.definitions, 0) + '</div></div>';
    }
    responsesHtml += '</div>';
  }

  const apiLabel = detail.title.replace('OPERA Cloud ', '').replace(' API', '');
  main.innerHTML =
    '<div class="breadcrumb"><a onclick="navigateApi(\'' + apiId + '\')">' + esc(apiLabel) + '</a> &rsaquo; Endpoint</div>' +
    '<div class="endpoint-detail">' +
    '<div class="endpoint-header"><span class="method-badge ' + ep.method + '">' + ep.method.toUpperCase() + '</span>' +
    '<span class="path">' + colorPath(ep.path) + '</span>' +
    '<button class="copy-btn" onclick="copyText(\'' + escAttr(ep.path) + '\')">Copy</button></div>' +
    '<div class="endpoint-summary">' + esc(ep.summary) + '</div>' +
    (cleanDesc ? '<div class="endpoint-desc">' + cleanDesc + '</div>' : '') +
    '<div class="endpoint-operation-id">Operation ID: <code>' + esc(ep.operationId) + '</code></div>' +
    paramsHtml + responsesHtml + '</div>';
  main.scrollTop = 0;
}

function renderSchema(schema, definitions, depth, seen) {
  if (!schema) return '';
  seen = seen || new Set();
  if (depth > 8) return '<span class="schema-type">...</span>';

  if (schema.$ref) {
    var refName = schema.$ref.replace('#/definitions/', '');
    return '<div class="schema-node">' +
      '<span class="schema-toggle" onclick="toggleSchemaRef(this, \'' + escAttr(refName) + '\')" data-ref="' + escAttr(refName) + '" data-depth="' + depth + '">&#9654;</span> ' +
      '<span class="schema-ref" onclick="toggleSchemaRef(this.previousElementSibling, \'' + escAttr(refName) + '\')">' + esc(refName) + '</span></div>';
  }

  if (schema.type === 'object' || schema.properties) {
    var props = schema.properties || {};
    var required = new Set(schema.required || []);
    var html = '<div class="schema-node"><span class="schema-type">object</span>';
    if (schema.description) html += ' <span class="schema-desc">\u2014 ' + esc(truncate(schema.description, 80)) + '</span>';
    html += '</div><div class="schema-children">';
    for (const [key, val] of Object.entries(props)) {
      html += '<div class="schema-node"><span class="schema-key">' + esc(key) + '</span>' + (required.has(key) ? ' <span class="required-badge">req</span>' : '') + ': ' + renderSchema(val, definitions, depth + 1, seen) + '</div>';
    }
    if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
      html += '<div class="schema-node"><span class="schema-key">[*]</span>: ' + renderSchema(schema.additionalProperties, definitions, depth + 1, seen) + '</div>';
    }
    html += '</div>';
    return html;
  }

  if (schema.type === 'array' && schema.items) {
    return '<span class="schema-type">array</span> of ' + renderSchema(schema.items, definitions, depth + 1, seen);
  }

  var typeStr = schema.type || 'any';
  if (schema.format) typeStr += ' (' + schema.format + ')';
  if (schema.enum) typeStr += ' [' + schema.enum.join(', ') + ']';
  var html = '<span class="schema-type">' + esc(typeStr) + '</span>';
  if (schema.description) html += ' <span class="schema-desc">\u2014 ' + esc(truncate(schema.description, 100)) + '</span>';
  return html;
}

function toggleSchemaRef(toggleEl, refName) {
  var node = toggleEl.closest('.schema-node');
  var childrenEl = node.querySelector('.schema-children');
  if (childrenEl) {
    childrenEl.remove();
    toggleEl.innerHTML = '&#9654;';
    return;
  }
  var detail = detailCache.get('rest:' + currentApiId);
  if (!detail) return;
  var def = detail.definitions[refName];
  if (!def) {
    var errDiv = document.createElement('div');
    errDiv.className = 'schema-children';
    errDiv.innerHTML = '<span class="schema-type">Definition not found</span>';
    node.appendChild(errDiv);
    return;
  }
  var depth = parseInt(toggleEl.dataset.depth || '0') + 1;
  var html = renderSchema(def, detail.definitions, depth);
  childrenEl = document.createElement('div');
  childrenEl.className = 'schema-children';
  childrenEl.innerHTML = html;
  node.appendChild(childrenEl);
  toggleEl.innerHTML = '&#9660;';
}

async function renderGraphqlOverview(schemaId) {
  var main = document.getElementById('main');
  var indexEntry = GRAPHQL_INDEX.find(function(s) { return s.id === schemaId; });
  if (!indexEntry) return;
  main.innerHTML = '<div class="loading"><div class="spinner"></div>Loading...</div>';
  var sdl = await getGraphqlDetail(schemaId);
  main.innerHTML =
    '<h2>' + esc(indexEntry.title || indexEntry.id) + '</h2>' +
    '<div class="api-meta">' +
    (indexEntry.version ? 'Version: ' + esc(indexEntry.version) + ' &middot; ' : '') +
    indexEntry.typeCounts.type + ' types &middot; ' + indexEntry.typeCounts.input + ' inputs &middot; ' + indexEntry.typeCounts.enum + ' enums' +
    (indexEntry.typeCounts.scalar ? ' &middot; ' + indexEntry.typeCounts.scalar + ' scalars' : '') +
    '</div>' +
    (indexEntry.desc ? '<div class="api-desc">' + esc(indexEntry.desc) + '</div>' : '') +
    (indexEntry.queryFields.length ? '<h3>Query Fields</h3><div class="tag-pills">' + indexEntry.queryFields.map(function(f) { return '<span class="tag-pill">' + esc(f) + '</span>'; }).join('') + '</div>' : '') +
    '<h3>Schema</h3><div class="graphql-sdl">' + highlightGraphQL(sdl) + '</div>';
  main.scrollTop = 0;
}

async function renderStreamingOverview() {
  var main = document.getElementById('main');
  if (!STREAMING_INDEX) return;
  main.innerHTML = '<div class="loading"><div class="spinner"></div>Loading...</div>';
  var data = await getStreamingDetail();
  main.innerHTML =
    '<h2>' + esc(STREAMING_INDEX.title) + '</h2>' +
    '<div class="api-meta">' + STREAMING_INDEX.typeCount + ' types</div>' +
    (STREAMING_INDEX.queryFields.length ? '<h3>Query Fields</h3><div class="tag-pills">' + STREAMING_INDEX.queryFields.map(function(f) { return '<span class="tag-pill">' + esc(f) + '</span>'; }).join('') + '</div>' : '') +
    (STREAMING_INDEX.subscriptionFields.length ? '<h3>Subscription Fields</h3><div class="tag-pills">' + STREAMING_INDEX.subscriptionFields.map(function(f) { return '<span class="tag-pill">' + esc(f) + '</span>'; }).join('') + '</div>' : '') +
    '<h3>Schema (Introspection)</h3><div class="graphql-sdl">' + esc(JSON.stringify(data, null, 2)) + '</div>';
}

function highlightGraphQL(sdl) {
  return sdl
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/(#[^\n]*)/g, '<span class="gql-comment">$1</span>')
    .replace(/\b(type|input|enum|scalar|union|interface|extend|schema|query|mutation|subscription|fragment|on|implements)\b/g, '<span class="gql-keyword">$1</span>')
    .replace(/(@\w+)/g, '<span class="gql-directive">$1</span>')
    .replace(/"([^"]*)"/g, '<span class="gql-string">"$1"</span>');
}

function fuzzyScore(query, haystack) {
  // Exact substring match gets highest score
  if (haystack.includes(query)) return 100 + (query.length / haystack.length) * 50;
  // Split query into tokens and check each
  var tokens = query.split(/[\s\/\-_]+/).filter(function(t) { return t.length > 0; });
  if (tokens.length === 0) return 0;
  var matched = 0;
  var totalPos = 0;
  for (var ti = 0; ti < tokens.length; ti++) {
    var pos = haystack.indexOf(tokens[ti]);
    if (pos >= 0) {
      matched++;
      totalPos += pos;
    } else {
      // Try camelCase split matching: "getRes" matches "getreservation"
      var found = false;
      for (var ci = 0; ci <= haystack.length - tokens[ti].length; ci++) {
        var sub = haystack.substring(ci, ci + tokens[ti].length);
        if (sub === tokens[ti]) { found = true; matched++; totalPos += ci; break; }
      }
      if (!found) {
        // Character-level fuzzy: all chars of token appear in order in haystack
        var hi = 0;
        var consecutive = 0;
        var maxConsecutive = 0;
        var charMatches = 0;
        for (var tci = 0; tci < tokens[ti].length; tci++) {
          while (hi < haystack.length && haystack[hi] !== tokens[ti][tci]) { hi++; consecutive = 0; }
          if (hi < haystack.length) { charMatches++; hi++; consecutive++; maxConsecutive = Math.max(maxConsecutive, consecutive); }
          else break;
        }
        if (charMatches === tokens[ti].length) {
          matched += 0.6 + (maxConsecutive / tokens[ti].length) * 0.3;
        }
      }
    }
  }
  if (matched === 0) return 0;
  // Score: proportion of tokens matched, penalize by position
  var score = (matched / tokens.length) * 60;
  score -= Math.min(totalPos * 0.01, 10);
  return score;
}

function shortPath(p, maxSegments) {
  maxSegments = maxSegments || 3;
  var parts = p.split('/').filter(function(s) { return s; });
  if (parts.length <= maxSegments) return colorPath(p);
  var tail = parts.slice(-maxSegments);
  return '<span class="ellipsis">\u2026/</span>' + tail.map(function(part, i) {
    var sep = i > 0 ? '<span class="sep">/</span>' : '';
    if (part.startsWith('{') && part.endsWith('}')) {
      return sep + '<span class="param">' + esc(part) + '</span>';
    }
    if (i === tail.length - 1) {
      return sep + '<span class="resource">' + esc(part) + '</span>';
    }
    return sep + '<span class="segment">' + esc(part) + '</span>';
  }).join('');
}

function pathWithTip(p, maxSegments) {
  var parts = p.split('/').filter(function(s) { return s; });
  if (parts.length <= (maxSegments || 3)) {
    return '<span class="path">' + colorPath(p) + '</span>';
  }
  return '<span class="path-wrap"><span class="path">' + shortPath(p, maxSegments) + '</span>' +
    '<span class="path-tip">' + colorPath(p) + '</span></span>';
}

function colorPath(p) {
  var parts = p.split('/');
  return parts.map(function(part, i) {
    if (!part && i === 0) return '';
    var sep = i > 0 ? '<span class="sep">/</span>' : '';
    if (part.startsWith('{') && part.endsWith('}')) {
      return sep + '<span class="param">' + esc(part) + '</span>';
    }
    if (i === parts.length - 1) {
      return sep + '<span class="resource">' + esc(part) + '</span>';
    }
    return sep + '<span class="segment">' + esc(part) + '</span>';
  }).join('');
}

function doSearch(query) {
  query = query.toLowerCase().trim();
  if (!query) { hideSearch(); return; }
  var scopedApiId = (searchScopedToApi && currentApiId && !currentApiId.startsWith('gql:') && currentApiId !== 'streaming') ? currentApiId : null;
  var scored = [];
  for (var ai = 0; ai < SEARCH_INDEX.length; ai++) {
    var api = SEARCH_INDEX[ai];
    if (scopedApiId && api.id !== scopedApiId) continue;
    for (var i = 0; i < api.endpoints.length; i++) {
      var ep = api.endpoints[i];
      var haystack = (ep.p + ' ' + ep.s + ' ' + ep.o).toLowerCase();
      var score = fuzzyScore(query, haystack);
      if (score > 20) {
        scored.push({ api: api, epIdx: i, ep: ep, score: score });
      }
    }
  }
  if (!scopedApiId) {
    for (var gi = 0; gi < GRAPHQL_INDEX.length; gi++) {
      var schema = GRAPHQL_INDEX[gi];
      var haystack = (schema.id + ' ' + schema.title + ' ' + schema.desc + ' ' + schema.queryFields.join(' ')).toLowerCase();
      var score = fuzzyScore(query, haystack);
      if (score > 20) {
        scored.push({ graphql: schema, score: score });
      }
    }
  }
  scored.sort(function(a, b) { return b.score - a.score; });
  var results = scored.slice(0, 50);
  searchResults = results;
  searchSelectedIdx = -1;
  var el = document.getElementById('searchResults');
  if (results.length === 0) {
    el.innerHTML = '<div style="padding:16px;color:var(--text-secondary);font-size:13px">No results found</div>';
    el.classList.add('active');
    document.getElementById('searchOverlay').style.display = 'block';
    return;
  }
  el.innerHTML = results.map(function(r, i) {
    if (r.graphql) {
      return '<div class="search-result-item" onclick="navigateGraphql(\'' + escAttr(r.graphql.id) + '\')" data-idx="' + i + '">' +
        '<span class="api-label">GraphQL</span>' +
        '<span class="path">' + esc(r.graphql.id) + '</span>' +
        '<span class="summary">' + esc(r.graphql.title) + '</span></div>';
    }
    var mc = methodFromShort(r.ep.m);
    return '<div class="search-result-item" onclick="navigateEndpoint(\'' + r.api.id + '\', ' + r.epIdx + ')" data-idx="' + i + '">' +
      '<span class="api-label">' + esc(r.api.id) + '</span>' +
      '<span class="method-badge ' + mc + '">' + r.ep.m + '</span>' +
      pathWithTip(r.ep.p, 3) +
      '<span class="summary">' + esc(r.ep.s) + '</span></div>';
  }).join('');
  el.classList.add('active');
  document.getElementById('searchOverlay').style.display = 'block';
}

function toggleSearchScope() {
  searchScopedToApi = !searchScopedToApi;
  updateSearchScope();
  var q = document.getElementById('searchInput').value;
  if (q) doSearch(q);
}

function updateSearchScope() {
  var badge = document.getElementById('searchScope');
  var input = document.getElementById('searchInput');
  if (searchScopedToApi && currentApiId && !currentApiId.startsWith('gql:') && currentApiId !== 'streaming') {
    var apiName = currentApiId;
    for (var i = 0; i < SEARCH_INDEX.length; i++) {
      if (SEARCH_INDEX[i].id === currentApiId) { apiName = SEARCH_INDEX[i].title || currentApiId; break; }
    }
    badge.textContent = '\u2715 ' + apiName;
    badge.title = 'Searching in ' + apiName + '. Click to search all APIs.';
    badge.classList.add('active');
    input.classList.add('scoped');
    input.placeholder = 'Search in ' + apiName + '... (press /)';
  } else {
    badge.classList.remove('active');
    input.classList.remove('scoped');
    input.placeholder = 'Search endpoints... (press /)';
  }
}

function hideSearch() {
  document.getElementById('searchResults').classList.remove('active');
  document.getElementById('searchOverlay').style.display = 'none';
  searchResults = [];
  searchSelectedIdx = -1;
}

function navigateApi(apiId) {
  activeTagFilter = null;
  currentApiId = apiId;
  currentEndpointIdx = null;
  expandedApis.clear();
  expandedApis.add(apiId);
  var api = SEARCH_INDEX.find(function(a) { return a.id === apiId; });
  if (api) expandedGroups.add(api.group);
  setHash('rest', apiId);
  renderSidebar();
  renderApiOverview(apiId);
  closeSidebar();
}

function navigateEndpoint(apiId, idx) {
  currentApiId = apiId;
  currentEndpointIdx = idx;
  expandedApis.add(apiId);
  var api = SEARCH_INDEX.find(function(a) { return a.id === apiId; });
  if (api) expandedGroups.add(api.group);
  setHash('rest', apiId, idx);
  renderSidebar();
  renderEndpointDetail(apiId, idx);
  hideSearch();
  closeSidebar();
}

function navigateGraphql(schemaId) {
  currentApiId = 'gql:' + schemaId;
  currentEndpointIdx = null;
  expandedGroups.add('__graphql__');
  setHash('graphql', schemaId);
  renderSidebar();
  renderGraphqlOverview(schemaId);
  closeSidebar();
}

function navigateStreaming() {
  currentApiId = 'streaming';
  currentEndpointIdx = null;
  setHash('streaming');
  renderSidebar();
  renderStreamingOverview();
  closeSidebar();
}

function toggleGroup(group) {
  if (expandedGroups.has(group)) expandedGroups.delete(group);
  else expandedGroups.add(group);
  renderSidebar();
}

function filterTag(tag) {
  activeTagFilter = tag;
  renderApiOverview(currentApiId);
}

function setHash(type, id, epIdx) {
  var h = '#/' + type;
  if (id) h += '/' + id;
  if (epIdx !== undefined && epIdx !== null) h += '/' + epIdx;
  if (location.hash !== h) history.pushState(null, '', h);
}

function handleHash() {
  var hash = location.hash.replace('#/', '');
  if (!hash) { renderWelcome(); renderSidebar(); return; }
  var parts = hash.split('/');
  if (parts[0] === 'rest' && parts[1]) {
    if (parts[2] !== undefined) navigateEndpoint(parts[1], parseInt(parts[2]));
    else navigateApi(parts[1]);
  } else if (parts[0] === 'graphql' && parts[1]) {
    navigateGraphql(parts[1]);
  } else if (parts[0] === 'streaming') {
    navigateStreaming();
  } else {
    renderWelcome();
  }
}

function esc(s) { if (!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function escAttr(s) { return esc(s).replace(/\\/g, '\\\\'); }
function truncate(s, n) { return s && s.length > n ? s.slice(0, n) + '...' : (s || ''); }
function stripHtml(s) { if (!s) return ''; return s.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(); }
function methodFromShort(m) { return {G:'get',P:'post',U:'put',A:'patch',D:'delete',H:'head',O:'options'}[m] || 'get'; }
function formatParamType(p) {
  var t = p.type || '';
  if (p.schema) {
    if (p.schema.$ref) return p.schema.$ref.replace('#/definitions/', '');
    if (p.schema.type) t = p.schema.type;
  }
  if (p.format) t += ' (' + p.format + ')';
  if (p.items) {
    if (p.items.type) t += '[' + p.items.type + ']';
    else if (p.items.$ref) t += '[' + p.items.$ref.replace('#/definitions/', '') + ']';
  }
  return t;
}
function copyText(text) { navigator.clipboard.writeText(text).catch(function() {}); }
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('overlay').classList.toggle('active');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('active');
}

document.getElementById('searchInput').addEventListener('input', function(e) {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(function() { doSearch(e.target.value); }, 120);
});
document.getElementById('searchInput').addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { hideSearch(); e.target.blur(); return; }
  var items = document.querySelectorAll('.search-result-item');
  if (e.key === 'ArrowDown') { e.preventDefault(); searchSelectedIdx = Math.min(searchSelectedIdx + 1, items.length - 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); searchSelectedIdx = Math.max(searchSelectedIdx - 1, 0); }
  else if (e.key === 'Enter' && searchSelectedIdx >= 0 && items[searchSelectedIdx]) { items[searchSelectedIdx].click(); return; }
  else return;
  items.forEach(function(it, i) { it.classList.toggle('selected', i === searchSelectedIdx); });
  if (items[searchSelectedIdx]) items[searchSelectedIdx].scrollIntoView({ block: 'nearest' });
});
document.addEventListener('click', function(e) {
  if (!e.target.closest('.search-wrapper')) hideSearch();
});
window.addEventListener('hashchange', handleHash);
document.addEventListener('keydown', function(e) {
  if (e.key === '/' && !e.target.closest('input')) {
    e.preventDefault();
    document.getElementById('searchInput').focus();
  }
});

handleHash();
</script>
</body>
</html>'''


def generate_html(rest_index, rest_details, graphql_index, graphql_details, streaming_index, streaming_detail):
    html = get_html_template()
    html = html.replace("__SEARCH_INDEX__", json.dumps(rest_index))
    html = html.replace("__REST_DETAIL__", json.dumps(rest_details))
    html = html.replace("__GRAPHQL_INDEX__", json.dumps(graphql_index))
    html = html.replace("__GRAPHQL_DETAIL__", json.dumps(graphql_details))
    html = html.replace("__STREAMING_INDEX__", json.dumps(streaming_index) if streaming_index else "null")
    html = html.replace("__STREAMING_DETAIL__", json.dumps(streaming_detail) if streaming_detail else "null")
    html = html.replace("__API_GROUPS__", json.dumps(API_GROUPS))
    return html


def main():
    parser = argparse.ArgumentParser(description="Generate OHIP API documentation HTML")
    parser.add_argument("--rest-specs", default="rest-api-specs/property/")
    parser.add_argument("--graphql-schemas", default="graphql/data-apis/")
    parser.add_argument("--streaming-schema", default="graphql/streaming/StreamingGraphQLSchema.json")
    parser.add_argument("--output", default="ohip-docs.html")
    args = parser.parse_args()

    print("Parsing REST API specs...")
    rest_index, rest_details = parse_rest_specs(Path(args.rest_specs))
    print("Parsing GraphQL schemas...")
    graphql_index, graphql_details = parse_graphql_schemas(Path(args.graphql_schemas))
    print("Parsing streaming schema...")
    streaming_index, streaming_detail = parse_streaming_schema(Path(args.streaming_schema))
    print("Generating HTML...")
    html = generate_html(rest_index, rest_details, graphql_index, graphql_details, streaming_index, streaming_detail)
    with open(args.output, "w") as f:
        f.write(html)
    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Generated {args.output} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()

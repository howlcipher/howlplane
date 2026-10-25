---
name: howlframe-app-development
description: Canonical guidance for building, modifying, debugging, testing, and reviewing fullstack and CLI applications written in HowlFrame (.howl). Use when developing or reviewing HowlFrame applications (e.g. backend services, HTTP APIs, web_app frontends, CLI tools, native store persistence, scripts/build.sh, and scripts/test.sh).
tier: 2
---

# HowlFrame Application Development

HowlFrame is an AI-native programming language and capability-bounded execution platform. Applications written in HowlFrame express domain logic, state management, HTTP serving, web interfaces, and CLI tooling using declarative S-expression syntax (`.howl`) compiled to standalone bytecode (`.hfbc`) and client JavaScript.

This skill is the engineering reference for building, maintaining, verifying, and reviewing applications written in HowlFrame.

---

## 1. Application Architecture

A canonical HowlFrame application consists of:

```text
HowlFrame Application Architecture:
=============================================================================
Browser / Client Layer:
  - Compiled from (web_app ...) -> static/app.js (vanilla JS)
  - Interacts with backend via REST JSON / RPC endpoints
-----------------------------------------------------------------------------
Server / Execution Runtime:
  - Compiled from (http_server port ...) or (cli_app ...) -> build/*.hfbc
  - Runs in HowlFrame Bytecode VM under strict capability boundaries:
    (network, database, filesystem, process)
-----------------------------------------------------------------------------
Persistence Layer:
  - Built-in Structured Record Store:
    (store_open kv "file://data/store.json")
    (store_get kv key), (store_put kv key dict), (store_delete kv key)
=============================================================================
```

---

## 2. Core Language Forms for Applications

### A. Root Forms
Every executable `.howl` file must declare a single root form:
* `(http_server <port> ...)`: Standalone HTTP server and API backend.
* `(web_app ...)`: Browser UI client logic (compiles to vanilla JavaScript).
* `(cli_app ...)`: Command-line tool or worker process.
* `(module ...)`: Reusable modular library imported with `(use "path/to/module.howl" as alias)`. Call an exported function as `(call alias/func args...)`. Do not write the dotted form `alias.func`: compilers with the howlframe#224 fix reject it at `check`, and older ones accepted it but built an artifact the VM refuses to load.

### B. Standard Expressions & Data Types
* **Functions & Flow:** `(defun name (args) ...)`, `(lambda (args) ...)`, `(let (var val) ...)`, `(set var val)`, `(do ...)` (sequential block), `(if cond then [else])`, `(return val)`. Untyped `defun` parameters and return values are `string`; declare other types in the body with `(type_hint x "int")` and `(type_hint return "void")`.
* **Error Handling:** `(try_let (var expr) (catch err handler) body)`: three operands; `body` runs with `var` bound when `expr` succeeds.
* **Collections:** `(dict ("key" val) ...)`, `(list item1 item2 ...)`, `(map_get dict "key")`, `(map_set dict "key" val)`, `(map_delete dict "key")`, `(append list item)` (a statement: it changes `list` in place and produces no value).
* **Strings & Conversion:** `(str_join list sep)`, `(str_split str sep)`, `(to_string val)`, `(to_int val)`, `(regex_match pattern str)`. `(read_file path)` returns bytes; convert with `(bytes_to_string ...)` before string operations.

---

## 3. Backend HTTP Services & APIs

Backend services declare routes and handlers inside `(http_server <port> ...)`:

```lisp
(http_server 8088

  ;; Route declarations
  (route "/api/notes" (lambda (req)
    (let (method (req_method req))
      (do
        (res_header "Access-Control-Allow-Origin" "*")
        (res_header "Access-Control-Allow-Headers" "Content-Type")
        (if (= method "GET")
          (do
            (store_open kv "file://data/notes.json")
            (let (note (store_get kv "1"))
              (res_json 200 (dict ("note" note)))
            )
          )
          (if (= method "POST")
            (let (body (req_body req))
              (try_let (payload (parse_json NoteInput body))
                (catch err
                  (res_json 400 (dict ("error" "Invalid JSON payload")))
                )
                (do
                  (store_open kv "file://data/notes.json")
                  (store_put kv "1" payload)
                  (res_json 201 (dict ("status" "created")))
                )
              )
            )
            (res 405 "text/plain" "Method Not Allowed")
          )
        )
      )
    )
  ))

  ;; Static asset serving
  (route "/" (lambda (req)
    (do
      (res_header "Content-Type" "text/html; charset=utf-8")
      (res 200 "text/html" (read_file "static/index.html"))
    )
  ))
)
```

### HTTP Primitives Reference:
* `(req_method req)`: Returns HTTP method string (`"GET"`, `"POST"`, `"PUT"`, `"DELETE"`, `"OPTIONS"`).
* `(req_body req)`: Returns request body as string.
* `(req_header req "Header-Name")`: Returns header value.
* `(res status_code mime_type body_string)`: Sends raw HTTP response.
* `(res_json status_code dict_or_list)`: Serializes dictionary/list to JSON response.
* `(res_header name value)`: Sets outgoing HTTP header on current response.

---

## 4. Native Record Store Persistence

HowlFrame provides a deterministic in-memory and file-backed structured record store:

```lisp
(do
  ;; Open persistent or in-memory store
  (store_open kv "file://data/records.json")

  ;; Insert or update record
  (store_put kv "user:100" (dict ("id" "100") ("name" "Alice") ("active" "true")))

  ;; Retrieve record (returns nil if not found)
  (let (rec (store_get kv "user:100"))
    (if (is_nil rec)
      (print "Record not found")
      (print (str_join (list "User: " (map_get rec "name")) ""))
    )
  )

  ;; Delete record
  (store_delete kv "user:100")
)
```

---

## 5. Frontend Web Applications

Frontend applications declare interactive UI logic inside `(web_app ...)`:

```lisp
(web_app
  (defun load_data ()
    (type_hint return "void")
    (do
      (let (status_banner (dom_query "#status"))
        (set_text status_banner "Loading...")
      )
      (try_let (raw_resp (fetch "/api/data" "GET"))
        (catch err
          (let (banner (dom_query "#status"))
            (set_text banner "Failed to load data")
          )
        )
        (try_let (parsed (parse_json DataResponse raw_resp))
          (catch json_err
            (print "JSON parse error")
          )
          (let (container (dom_query "#content"))
            (set_attr container "class" "loaded")
          )
        )
      )
    )
  )
)
```

### Web Primitives Reference:
* `(dom_query selector)`: Queries a DOM node.
* `(set_text node text)`: Updates inner text of a node.
* `(set_attr node attr value)`: Updates DOM attribute or style.
* `(dom_value node)`: Retrieves value of an input element.
* `(fetch url method [body])`: Performs HTTP fetch request.
* `(parse_json TypeHint json_str)`: Parses JSON string into dictionary structure.

---

## 6. Build, Test, and Verification Standards

HowlFrame applications must follow strict deterministic verification contracts:

### A. Directory Structure
```text
my-howl-app/
├── app/
│   ├── backend.howl      # HTTP server / domain backend
│   └── frontend.howl     # Optional web_app frontend
├── build/                # Compiled .hfbc bytecode artifacts
├── data/                 # File-backed store databases
├── scripts/
│   ├── build.sh          # Canonical compilation script
│   ├── test.sh           # Canonical E2E / integration test script
│   └── run.sh            # Local server runner
├── static/               # HTML/CSS and compiled app.js
└── tests/
    └── e2e_test.go       # Go or Bash end-to-end tests
```

### B. Standard Scripts
1. **`scripts/build.sh`:**
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   mkdir -p build static
   howlframe build app/backend.howl -o build/backend.hfbc
   if [ -f app/frontend.howl ]; then
     howlframe build app/frontend.howl --target js -o static/app.js
   fi
   ```

2. **`scripts/test.sh`:**
   Executes automated tests covering compilation, capability denials, and full HTTP/CLI CRUD behavior.

---

## 7. Reviewer Checklist for HowlFrame Applications

When reviewing changes to HowlFrame applications, verify:

1. **Syntax & Balanced Parentheses:** All S-expressions are properly balanced.
2. **Single Root Form:** Exactly one root form (`http_server`, `web_app`, `cli_app`, or `module`) per file.
3. **Capability Grants:** Runtime grants (`howlframe run --allow-caps network,database,filesystem ...`) match application requirements; `howlframe inspect --caps build/backend.hfbc` lists what an artifact needs.
4. **Failure Closed:** All input parsing (`parse_json`), file operations (`read_file`), and network calls (`fetch`) use `try_let` or error checking. `write_file` produces no value, so wrap it to catch failures: `(try_let (ok (do (write_file path data) "ok")) (catch err ...) body)`.
5. **No Transpiler Drift:** Bytecode builds succeed with clean exit code 0 against the pinned compiler version.
6. **Deterministic Verification:** `bash scripts/build.sh` and `bash scripts/test.sh` execute and pass cleanly.

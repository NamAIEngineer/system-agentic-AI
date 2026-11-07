# Repository status

Checked: 2026-11-07 (Asia/Ho_Chi_Minh)

This file records the public reference repositories used by the framework. The
cloneable bundle does not vendor these checkouts. `scripts/install-references.py`
can clone a selected pinned revision into a user-local references directory only
after explicit `--install --allow-network` opt-in.

| Component | Upstream | Latest remote `main` checked | Local checkout at inspection | State |
| --- | --- | --- | --- | --- |
| System One / Laya / JEV | [jangtrinh/design-os-system-one](https://github.com/jangtrinh/design-os-system-one) | `ca2268f1ac24beace62815294fbf87fc2b7e4f15` | `263246f1a7f081792c4b7d77d0dadf0587bde59c` | remote newer; local had 2 uncommitted hardening changes and `REPORT.md` |
| Orchestration System by Jang | [jangtrinh/orchestration-system-by-jang](https://github.com/jangtrinh/orchestration-system-by-jang) | `8b1ad04245a5ac9882ad98dc00c835ce0ee0adff` | `0e75677d574b492f5add91bf7024594c865f3855` | remote newer; local had untracked `REPORT.md` |
| gstack | [garrytan/gstack](https://github.com/garrytan/gstack) | `e75a69c9e15164d090e080fa001dc515495ec350` | `29f8ca053e7417d4702a094ce867f00cb7081a46` | local behind 34 commits; local had untracked report and `.DS_Store` |
| Pedagogy | [jangtrinh/design-os-pedagogy](https://github.com/jangtrinh/design-os-pedagogy) | `9753b24edbb4941688171e9af40ad2759901af4f` | local ref `3a053d281fb55f4fd5ba31c03c291d99e45a0b3d` | local object database damaged; use fresh clone |
| Knowledge Builder | [jangtrinh/design-os-knowledge-builder](https://github.com/jangtrinh/design-os-knowledge-builder) | `824503cde9a349349526d65e02396a00f7985c73` | `3662003b2b1e78c9ddfef22893b32e7ba611694c` | remote newer; local had untracked `REPORT.md` |
| Caveman | [JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman) | `c310c9d300df13a1dd9f5c086120ffd857a164a1` | local ref `d262ab3882952a59312ff3537cd86d894523c5ab` | local object database damaged; use fresh clone |
| REA provider | [morluto/rea](https://github.com/morluto/rea) | checked live during setup | pinned by the provider registry | cloned under `third_party/rea`; read-only provider |

The current public bundle intentionally excludes product repositories, project
plans, generated artifacts, runtime databases, logs, model weights, credentials,
and nested Git histories. Those remain in the private working folder.

The root GitHub destination is [NamAIEngineer/system-agentic-AI](https://github.com/NamAIEngineer/system-agentic-AI).

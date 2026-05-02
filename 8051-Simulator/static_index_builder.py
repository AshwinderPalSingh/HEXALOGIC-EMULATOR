from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_SHELL_PARTIAL = ROOT / "api" / "templates" / "_app_shell.html"
STATIC_INDEX = ROOT / "index.html"
STATIC_ASSET_PREFIX = "/api/static"


def build_static_index_html(*, asset_prefix: str = STATIC_ASSET_PREFIX) -> str:
    shell_markup = APP_SHELL_PARTIAL.read_text().replace("{{ asset_prefix }}", asset_prefix)
    return "\n".join(
        [
            "<!DOCTYPE html>",
            "<html lang=\"en\">",
            "",
            "<head>",
            "    <meta charset=\"UTF-8\">",
            "    <meta property=\"og:title\" content=\"HexaLogic\" />",
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">",
            f"    <link rel=\"stylesheet\" href=\"{asset_prefix}/styles.css\">",
            "    <title>HexaLogic | Multi-Architecture Emulator</title>",
            "    <script>",
            "        window.HEXLOGIC_API_BASE = \"/api/v2\";",
            "        window.HEXLOGIC_MONACO_BASE = \"https://unpkg.com/monaco-editor@0.45.0/min/\";",
            "    </script>",
            "    <script src=\"https://unpkg.com/monaco-editor@0.45.0/min/vs/loader.js\"></script>",
            "    <script>",
            "        if (typeof window.require === \"undefined\") {",
            "            console.error(\"Monaco AMD loader did not load from CDN.\");",
            "        } else {",
            "            window.require.config({",
            "                paths: {",
            "                    vs: \"https://unpkg.com/monaco-editor@0.45.0/min/vs\"",
            "                }",
            "            });",
            "        }",
            "    </script>",
            "    <script type=\"module\" src=\"/src/main.js\"></script>",
            "</head>",
            "",
            "<body>",
            shell_markup,
            "</body>",
            "",
            "</html>",
            "",
        ]
    )


def write_static_index(path: Path = STATIC_INDEX) -> Path:
    path.write_text(build_static_index_html())
    return path


if __name__ == "__main__":
    write_static_index()

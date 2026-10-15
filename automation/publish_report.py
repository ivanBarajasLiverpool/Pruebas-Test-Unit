"""Publish only validated status fields, never model or test output."""

import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

MARKER = "<!-- unit-test-agent -->"
STATUSES = {
    "passed": "Pruebas propuestas validadas",
    "no_changes": "Sin cambios Python elegibles",
    "ambiguous": "Especificación ambigua: se requiere revisión humana",
    "baseline_failed": "La suite existente no pasa; no se generaron pruebas",
    "tests_failed": "Las pruebas propuestas no pasaron",
    "error": "No se pudo completar la generación",
}


def summary(report, run_url):
    status = report.get("status", "error")
    if status not in STATUSES:
        status = "error"
    attempts = report.get("attempts", 0)
    count = report.get("generated_files", 0)
    if type(attempts) is not int or not 0 <= attempts <= 3:
        attempts = 0
    if type(count) is not int or not 0 <= count <= 100:
        count = 0
    return (
        f"{MARKER}\n### Agente de pruebas unitarias\n"
        f"**{STATUSES[status]}**\n\n"
        f"Intentos: {attempts}. Archivos propuestos: {count}.\n\n"
        f"[Resultados y artefactos]({run_url})\n\n"
        "Solo el parche `proposed-tests.patch` de la raíz del artefacto "
        "ha pasado la validación. Los parches de cada intento son candidatos "
        "no aprobados. No se modificó la rama del PR.\n"
    )


def request(method, path, body=None):
    endpoint = "https://api.github.com"
    req = Request(
        endpoint + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    number = os.environ["PR_NUMBER"]
    run_id = os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not number.isdigit() or not run_id.isdigit():
        raise ValueError("Invalid run/PR")
    path = Path(os.environ["REPORT_PATH"])
    report = {}
    if path.is_file() and path.stat().st_size <= 32000:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    if not isinstance(report, dict):
        report = {}
    body = summary(report, f"https://github.com/{repository}/actions/runs/{run_id}")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(body)
    # Cancellation stops stale runs before publication; update rather than spam.
    for page in range(1, 11):
        comments = request(
            "GET",
            f"/repos/{repository}/issues/{number}/comments?per_page=100&page={page}",
        )
        for comment in comments:
            if comment.get("user", {}).get(
                "login"
            ) == "github-actions[bot]" and comment.get("body", "").startswith(MARKER):
                request(
                    "PATCH",
                    f"/repos/{repository}/issues/comments/{comment['id']}",
                    {"body": body},
                )
                return
        if len(comments) < 100:
            break
    request("POST", f"/repos/{repository}/issues/{number}/comments", {"body": body})


if __name__ == "__main__":
    main()

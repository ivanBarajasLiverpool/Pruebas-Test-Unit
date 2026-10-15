# Pruebas-Test-Unit

Agente central para **proponer nuevas pruebas Python/pytest** en cada pull request.
La implementación usa únicamente la biblioteca estándar y la API de OpenAI.
No modifica código productivo, pruebas existentes ni la rama del autor.

## Activación en este repositorio

1. Integra estos archivos en la rama base. La primera ejecución necesita que el
   agente y `unit-test-agent.json` ya existan en esa rama: nunca se ejecuta la
   implementación del agente aportada por el PR.
2. Configura el secreto de Actions `UNIT_TEST_AGENT_API_KEY` con una credencial de
   proyecto limitada al modelo elegido y con presupuesto y alertas del proveedor.
   Revisa previamente la autorización para enviar código a ese proveedor.
3. Permite GitHub Actions y que los workflows publiquen comentarios en PR.
4. Abre o actualiza un PR interno que modifique `Calculadora`. En Actions aparecerá
   el artefacto `unit-test-agent-<número>` y un comentario del bot con el resultado.

El piloto analiza `agent-starter-pack-main/Calculadora` y ejecuta su suite en
Python 3.11. Se eliminaron los dos workflows duplicados e inactivos del starter
pack que invocaban un `generate_tests.py` inexistente.

## Reutilización en otro repositorio

El repositorio central mantiene el agente, la imagen de validación y
`.github/workflows/unit-test-agent.yml` (`workflow_call`). Cada consumidor necesita:

- Un `unit-test-agent.json` en su rama base, adaptado a sus rutas y comandos.
- Un workflow de entrada en `.github/workflows/`.
- El secreto del modelo, compartido mediante secretos de organización con acceso
  limitado a los repositorios autorizados, o configurado por repositorio.

Ejemplo de workflow consumidor; sustituye **ambos** `SHA_DE_RELEASE` por el mismo
SHA completo de un commit revisado de este repositorio, después de integrar esta
implementación:

```yaml
name: Propose unit tests
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
  actions: read
  pull-requests: write
concurrency:
  group: unit-test-agent-${{ github.repository }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  tests:
    uses: ivanBarajasLiverpool/Pruebas-Test-Unit/.github/workflows/unit-test-agent.yml@SHA_DE_RELEASE
    with:
      agent-ref: SHA_DE_RELEASE
      config-path: unit-test-agent.json
    secrets:
      UNIT_TEST_AGENT_API_KEY: ${{ secrets.UNIT_TEST_AGENT_API_KEY }}
```

Si el repositorio central es privado, habilita su acceso como workflow reutilizable
en la configuración de Actions. Además, proporciona
`UNIT_TEST_AGENT_SOURCE_TOKEN` como secreto del workflow: un token de instalación
de una GitHub App con permiso **Contents: read** sobre el repositorio central.
El `GITHUB_TOKEN` del consumidor no puede leer por sí solo otro repositorio
privado. La emisión/renovación de ese token corresponde a la integración de la
organización; no se incluye un emisor de tokens ni se habilitan commits automáticos.

No se ha habilitado un segundo repositorio desde aquí; el workflow y la
configuración constituyen el contrato para incorporarlo.

## Configuración

`unit-test-agent.json` es configuración de confianza: se obtiene con `git show`
desde el SHA base del PR, no desde la versión modificada en el PR.
Para cambiarla, revisa e integra primero el cambio.

| Campo | Propósito |
| --- | --- |
| `version`, `language`, `python_version` | Esquema 1; este piloto admite Python/pytest con Python 3.11. |
| `project_directory` | Raíz del proyecto relativa al repositorio (`.` si corresponde). |
| `tests_directory` | Directorio permitido de nuevas pruebas, relativo al proyecto. |
| `source_paths` | Archivos/directorios fuente relativos al proyecto; las pruebas se excluyen automáticamente. |
| `exclude_paths` | Patrones glob relativos al proyecto que no se envían al modelo. |
| `install_command`, `test_command` | Comandos revisados, ejecutados exclusivamente dentro del contenedor. |
| `model` | Modelo compatible con Chat Completions y respuesta JSON; predeterminado `gpt-4.1-mini`. |
| `max_attempts` | Entre 1 y 3 propuestas completas; incluye el intento inicial. |
| `max_context_bytes`, `max_file_bytes`, `max_files` | Presupuesto de contexto y selección de archivos. |
| `max_output_bytes`, `max_completion_tokens` | Límites de salida y tokens por llamada. |

Los límites acotan consumo, pero **no garantizan un costo monetario**; configura
también límites del proveedor. Solo se envían fuentes elegibles, dependencias
locales y pruebas/documentación seleccionadas dentro del presupuesto. No se envía
el repositorio completo. Se rechazan rutas inseguras y enlaces simbólicos.

La imagen confiable incluye pytest. La instalación y ejecución del proyecto son
**offline**, sin red. Para proyectos con dependencias adicionales, publica una
nueva versión revisada del agente con esas dependencias preinstaladas en su imagen
o incluye un wheelhouse revisado y usa instalación offline. No habilites red ni
credenciales en el sandbox para instalar dependencias del PR.

El comando piloto ejecuta toda la suite de `Calculadora`, no las pruebas del
starter pack ajenas a ese módulo. En cada consumidor, configura `test_command`
para cubrir todas las pruebas existentes relevantes y las pruebas generadas.

## Ciclo y resultados

1. Lee el PR por SHA, valida la configuración base y crea una copia de archivos
   versionados sin `.git` ni credenciales.
2. Ejecuta primero las pruebas existentes. Si fallan, informa `baseline_failed`
   sin solicitar nuevas pruebas al modelo. El código 5 de pytest (sin pruebas
   recolectadas) se admite únicamente para esa línea base; la propuesta final
   siempre debe terminar con código 0.
3. Analiza los cambios del PR, contratos y contexto; si el comportamiento esperado
   es ambiguo, devuelve `ambiguous` y solicita revisión humana.
4. Genera únicamente archivos nuevos `test_*.py` dentro del directorio permitido.
5. Ejecuta instalación y suite sobre una copia de solo lectura, con usuario no
   privilegiado, sin red, sin secretos, sin socket Docker y con límites de
   memoria, procesos, CPU, tiempo y logs.
6. Ante fallos, devuelve al modelo los logs acotados y la propuesta anterior,
   hasta agotar `max_attempts`. Cada intento empieza desde el código original.
7. Publica el reporte y los candidatos como artefactos; otro job, sin ejecutar
   código del proyecto, actualiza el comentario del bot.

**Solo `proposed-tests.patch` en la raíz del artefacto pasó la suite.** Los parches
de `attempt-*` son propuestas, no resultados aprobados. Revisa el parche antes de
aplicarlo con `git apply`; que pase la suite no demuestra que sus aserciones sean
correctas o que aumente la cobertura. Las explicaciones de ambigüedad se conservan
en el reporte del intento, no se renderizan como instrucciones en el comentario.
Los artefactos, que pueden contener código y logs del proyecto, duran siete días.

Los PR de forks y borradores se omiten sin recibir secretos; también se omiten
actualizaciones iniciadas por `github-actions[bot]`. No se usa
`pull_request_target`. Para un fork, revisa el código y llévalo a una rama interna
autorizada si necesitas generación. No añadas secretos al contexto del PR.
Los eventos habilitados son `opened`, `synchronize` y `reopened`; al sacar un
borrador de ese estado se requiere una actualización o reapertura del PR.

La separación entre modelo y pruebas evita ejecutar código del PR con el secreto
del modelo. El contenedor es una defensa operativa, no una garantía frente a
vulnerabilidades del kernel: usa runners efímeros hospedados y no runners
autohospedados compartidos con sistemas sensibles.

## Validación local

Con las dependencias pytest existentes instaladas:

```bash
python -m pytest tests -q
cd agent-starter-pack-main
python -m pytest Calculadora/tests -q
```

Prueba opcional de aislamiento real:

```bash
docker build -t unit-test-agent:local automation
TEST_UNIT_AGENT_DOCKER=1 python -m pytest tests/test_unit_test_runner.py -q
```

Las pruebas del agente simulan el proveedor: no requieren secretos ni llamadas
pagadas. El flujo completo con el modelo necesita un PR interno, configuración
ya integrada y una credencial válida; no se simula una ejecución real de OpenAI.
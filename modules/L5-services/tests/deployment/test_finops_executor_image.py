"""The private executor builds and checks its runtime as the image's non-root user."""

import shlex
from pathlib import Path


MODULE = Path(__file__).resolve().parents[2]


def test_executor_image_checks_runtime_access_after_selecting_non_root_user():
    source = (MODULE / "infra/finops/Dockerfile.executor").read_text()
    instructions = [line.strip() for line in source.replace("\\\n", "").splitlines() if line.strip()]
    users = [index for index, line in enumerate(instructions) if line.startswith("USER ")]
    assert len(users) == 1 and instructions[users[0]] == "USER pwuser"
    install = next(index for index, line in enumerate(instructions) if line.startswith("RUN python -m pip install"))
    check = next(index for index, line in enumerate(instructions) if line.startswith("RUN python -c"))
    entrypoint = next(index for index, line in enumerate(instructions) if line.startswith("ENTRYPOINT "))
    assert install < users[0] < check < entrypoint
    command = shlex.split(instructions[check])
    assert command[:3] == ["RUN", "python", "-c"] and len(command) == 4
    code = command[3]
    compile(code, "finops-image-runtime-check", "exec")
    assert "assert os.geteuid() != 0" in code
    assert "awslambdaric" in code and "from l5_services.adapters.finops.executor_host import lambda_handler" in code
    assert "callable(lambda_handler)" in code
    assert "tempfile.TemporaryFile(dir='/tmp')" in code
    assert "scratch.write(b'finops')" in code and "assert scratch.read() == b'finops'" in code
    assert "from playwright.sync_api import sync_playwright" in code
    assert "os.access(browser.chromium.executable_path, os.R_OK | os.X_OK)" in code
    assert "browser.stop()" in code
    assert instructions[entrypoint] == 'ENTRYPOINT ["python", "-m", "awslambdaric"]'
    assert instructions[-1] == 'CMD ["l5_services.adapters.finops.executor_host.lambda_handler"]'

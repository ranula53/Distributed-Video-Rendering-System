# Present so `import pp` resolves when running pytest from render-ui/.
import pytest
import pp


@pytest.fixture(autouse=True)
def _reset_job_state():
    pp._job_state = None
    pp._resume_thread = None
    yield
    pp._job_state = None
    pp._resume_thread = None

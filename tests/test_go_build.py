import subprocess
import os


def test_go_code_compiles_successfully():
    """
    Regression test to ensure the Go package compiles correctly.
    Prevents errors like 'undefined: T' caused by ignoring multiple files in a package.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # Run the exact build command used in cross_platform.yml
    result = subprocess.run(
        ["go", "build", "-o", os.devnull, "./cmd/howlplane"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert (
        result.returncode == 0
    ), f"Go compilation failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def test_go_code_is_formatted():
    """
    Regression test to ensure all Go files are perfectly formatted using gofmt.
    Prevents golangci-lint from failing the CI pipeline unexpectedly.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    result = subprocess.run(
        ["gofmt", "-l", "."],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    unformatted_files = result.stdout.strip()
    assert result.returncode == 0, f"gofmt execution failed: {result.stderr}"
    assert (
        unformatted_files == ""
    ), f"The following Go files are not formatted properly. Run 'make format' or 'gofmt -w .':\n{unformatted_files}"

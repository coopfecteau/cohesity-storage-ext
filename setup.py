from pathlib import Path

from setuptools import find_packages, setup


def find_version() -> str:
    """Keep the wheel version in step with extension.yaml, which is the source of truth."""
    version = "0.0.1"
    extension_yaml_path = Path(__file__).parent / "extension" / "extension.yaml"
    try:
        with open(extension_yaml_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("version"):
                    version = line.split(" ")[-1].strip('"').strip()
                    break
    except Exception:
        pass
    return version


setup(
    name="cohesity_storage",
    version=find_version(),
    description="Poll a Cohesity cluster REST API and report storage and protection metrics to Dynatrace",
    author="Cooper Fecteau",
    packages=find_packages(exclude=["tests", "tests.*"]),
    # ActiveGate 1.347 stopped shipping the 3.10 interpreter, so 3.14 is the only target.
    python_requires=">=3.14",
    include_package_data=True,
    # No HTTP client dependency on purpose: the skeleton reaches the cluster with stdlib
    # urllib, so nothing has to be vendored into extension/lib yet. Ticket 07 decides
    # whether the real client is worth pulling requests (and urllib3, certifi) in for.
    install_requires=["dt-extensions-sdk>=1.10.1"],
    extras_require={"dev": ["dt-extensions-sdk[cli]>=1.10.1", "pytest"]},
)

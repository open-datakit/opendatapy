"""Helpers for executing datapackages and loading and writing resources"""

import json
import os
import time
from docker import DockerClient

from .helpers import find_by_name
from .resources import TabularDataResource


DEFAULT_BASE_PATH = os.getcwd()  # Default base datapackage path
ALGORITHMS_DIR = "algorithms"
CONFIGURATIONS_DIR = "configurations"
RESOURCES_DIR = "resources"
FORMATS_DIR = "formats"
VIEWS_DIR = "views"


class ExecutionError(Exception):
    def __init__(self, message, logs):
        super().__init__(message)
        self.logs = logs


class ResourceError(Exception):
    def __init__(self, message, resource):
        super().__init__(message)
        self.resource = resource


def execute_datapackage(
    docker_client: DockerClient,
    configuration_name: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> str:
    """Execute a datpackage and return execution logs"""
    # Get execution container name from the configuration
    container_name = load_configuration(configuration_name, base_path)[
        "container"
    ]

    return execute_container(
        docker_client=docker_client,
        container_name=container_name,
        environment={
            "CONFIGURATION": configuration_name,
        },
        base_path=base_path,
    )


def execute_view(
    docker_client: DockerClient,
    view_name: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> str:
    """Execute a view and return execution logs"""
    view = load_view(view_name, base_path)

    # Check required resources are populated
    for resource_name in view["resources"]:
        with open(
            f"{base_path}/{RESOURCES_DIR}/{resource_name}.json", "r"
        ) as f:
            if not json.load(f)["data"]:
                raise ResourceError(
                    (
                        f"Can't render view with empty resource "
                        f"{resource_name}. Have you executed the datapackage?"
                    ),
                    resource=resource_name,
                )

    # Get container name from view
    container_name = view["container"]

    # Execute view
    return execute_container(
        docker_client=docker_client,
        container_name=container_name,
        environment={
            "VIEW": view_name,
        },
        base_path=base_path,
    )


def execute_container(
    docker_client: DockerClient,
    container_name: str,
    environment: dict,
    base_path: str = DEFAULT_BASE_PATH,
) -> str:
    """Execute a container"""
    # We have to detach to get access to the container object and its logs
    # in the event of an error
    container = docker_client.containers.run(
        image=container_name,
        volumes=[f"{base_path}:/usr/src/app/datapackage"],
        environment=environment,
        detach=True,
        user=os.getuid(),  # Run as current user (avoid permissions issues)
    )

    # Block until container is finished running
    result = container.wait()

    if result["StatusCode"] != 0:
        raise ExecutionError(
            "Execution failed with status code {result['StatusCode']}",
            logs=container.logs().decode("utf-8").strip(),
        )

    return container.logs().decode("utf-8").strip()


def load_view(
    view_name: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> dict:
    """Load a view"""
    with open(f"{base_path}/{VIEWS_DIR}/{view_name}.json", "r") as f:
        return json.load(f)


def load_configuration(
    configuration_name: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> dict:
    """Load a run configuration"""
    with open(
        f"{base_path}/{CONFIGURATIONS_DIR}/{configuration_name}.json", "r"
    ) as f:
        return json.load(f)


def write_configuration(
    configuration: dict,
    base_path: str = DEFAULT_BASE_PATH,
) -> dict:
    """Write a run configuration"""
    with open(
        f"{base_path}/{CONFIGURATIONS_DIR}/{configuration['name']}.json",
        "w",
    ) as f:
        json.dump(configuration, f, indent=2)


def load_resource(
    resource_name: str,
    format_name: str | None = None,
    base_path: str = DEFAULT_BASE_PATH,
    as_dict: bool = False,  # Load resource as raw dict
) -> TabularDataResource | dict:
    """Load a resource with the specified format"""
    # Load resource with format
    resource_path = f"{base_path}/{RESOURCES_DIR}/{resource_name}.json"

    resource = None

    with open(resource_path, "r") as resource_file:
        # Load resource object
        resource_json = json.load(resource_file)

        if format_name is not None:
            # Load format into resource object
            with open(
                f"{base_path}/{FORMATS_DIR}/{format_name}.json", "r"
            ) as format_file:
                resource_json["format"] = json.load(format_file)["schema"]

            # Copy format to resource schema if specified
            if resource_json["schema"] == "inherit-from-format":
                # Copy format to schema
                resource_json["schema"] = resource_json["format"]
                # Label schema as format copy so we don't overwrite it when
                # writing back to resource
                resource_json["schema"]["type"] = "format"
        else:
            # TODO: Temporary mostly harmless hack in order to be able
            # to load resource data into views, where we don't know or care
            # about the format
            # Longer-term we should deal with this by handling empty formats
            # in TabularDataResources, but this will do for now
            resource_json["format"] = {"hello": "world"}

        if as_dict:
            resource = resource_json
        elif (
            resource_json["profile"] == "tabular-data-resource"
            or resource_json["profile"] == "parameter-tabular-data-resource"
        ):
            # TODO: Create ParameterResource object to handle parameters
            resource = TabularDataResource(resource=resource_json)
        else:
            raise NotImplementedError(
                f"Unknown resource profile \"{resource_json['profile']}\""
            )

    return resource


def load_resource_by_variable(
    variable_name: str,
    configuration_name: str,
    base_path: str,
    as_dict: bool = False,  # Load resource as raw dict
) -> TabularDataResource | dict:
    """Convenience function for loading resource associated with a variable"""
    # Load configuration to get resource and format names
    configuration = load_configuration(configuration_name, base_path=base_path)

    variable = find_by_name(configuration["data"], variable_name)

    if variable is None:
        raise KeyError(
            (
                f"Can't find variable named {variable_name} in configuration "
                f"{configuration_name}"
            )
        )

    return load_resource(
        resource_name=variable["resource"],
        format_name=variable["format"],
        base_path=base_path,
        as_dict=as_dict,
    )


def write_resource(
    resource: TabularDataResource | dict,
    base_path: str = DEFAULT_BASE_PATH,
) -> None:
    """Write updated resource to file"""
    if isinstance(resource, TabularDataResource):
        resource_json = resource.to_dict()
    else:
        resource_json = resource

    resource_path = f"{base_path}/{RESOURCES_DIR}/{resource_json['name']}.json"

    # Remove format before writing
    # This should have been loaded by load_resource
    resource_json.pop("format")

    if resource_json["schema"].get("type") == "format":
        # Don't write format copy to schema
        resource_json["schema"] = "inherit-from-format"

    with open(resource_path, "w") as f:
        json.dump(resource_json, f, indent=2)

    # Update modified time in datapackage.json
    with open(f"{base_path}/datapackage.json", "r") as f:
        dp = json.load(f)

    dp["updated"] = int(time.time())

    with open(f"{base_path}/datapackage.json", "w") as f:
        json.dump(dp, f, indent=2)

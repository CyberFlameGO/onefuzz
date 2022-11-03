#!/usr/bin/env python
#
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import logging
import os
import pathlib
from typing import Dict, List, Optional

from onefuzztypes.enums import Compare, ContainerPermission, ContainerType, TaskFeature
from onefuzztypes.models import Job, Task, TaskConfig, TaskDefinition, TaskUnitConfig
from onefuzztypes.primitives import Container

from ..azure.containers import (
    add_container_sas_url,
    blob_exists,
    container_exists,
    get_container_sas_url,
)
from ..azure.creds import get_instance_id
from ..azure.queue import get_queue_sas
from ..azure.storage import StorageType
from ..workers.pools import Pool
from .defs import TASK_DEFINITIONS

LOGGER = logging.getLogger("onefuzz")


def get_input_container_queues(config: TaskConfig) -> Optional[List[str]]:  # tasks.Task

    if config.task.type not in TASK_DEFINITIONS:
        raise TaskConfigError("unsupported task type: %s" % config.task.type.name)

    container_type = TASK_DEFINITIONS[config.task.type].monitor_queue
    if container_type:
        return [x.name for x in config.containers if x.type == container_type]
    return None


def check_val(compare: Compare, expected: int, actual: int) -> bool:
    if compare == Compare.Equal:
        return expected == actual

    if compare == Compare.AtLeast:
        return expected <= actual

    if compare == Compare.AtMost:
        return expected >= actual

    raise NotImplementedError


def check_container(
    compare: Compare,
    expected: int,
    container_type: ContainerType,
    containers: Dict[ContainerType, List[Container]],
) -> None:
    actual = len(containers.get(container_type, []))
    if not check_val(compare, expected, actual):
        raise TaskConfigError(
            "container type %s: expected %s %d, got %d"
            % (container_type.name, compare.name, expected, actual)
        )


def check_containers(definition: TaskDefinition, config: TaskConfig) -> None:
    checked = set()

    containers: Dict[ContainerType, List[Container]] = {}
    for container in config.containers:
        if container.name not in checked:
            if not container_exists(container.name, StorageType.corpus):
                raise TaskConfigError("missing container: %s" % container.name)
            checked.add(container.name)

        if container.type not in containers:
            containers[container.type] = []
        containers[container.type].append(container.name)

    for container_def in definition.containers:
        check_container(
            container_def.compare, container_def.value, container_def.type, containers
        )

    for container_type in containers:
        if container_type not in [x.type for x in definition.containers]:
            raise TaskConfigError(
                "unsupported container type for this task: %s" % container_type.name
            )

    if definition.monitor_queue:
        if definition.monitor_queue not in [x.type for x in definition.containers]:
            raise TaskConfigError(
                "unable to monitor container type as it is not used by this task: %s"
                % definition.monitor_queue.name
            )


def check_target_exe(config: TaskConfig, definition: TaskDefinition) -> None:
    if config.task.target_exe is None:
        if TaskFeature.target_exe in definition.features:
            raise TaskConfigError("missing target_exe")

        if TaskFeature.target_exe_optional in definition.features:
            return

        return

    # User-submitted paths must be relative to the setup directory that contains them.
    # They also must be normalized, and exclude special filesystem path elements.
    #
    # For example, accessing the blob store path "./foo" generates an exception, but
    # "foo" and "foo/bar" do not.
    if not is_valid_blob_name(config.task.target_exe):
        raise TaskConfigError("target_exe must be a canonicalized relative path")

    container = [x for x in config.containers if x.type == ContainerType.setup][0]
    if not blob_exists(container.name, config.task.target_exe, StorageType.corpus):
        err = "target_exe `%s` does not exist in the setup container `%s`" % (
            config.task.target_exe,
            container.name,
        )
        LOGGER.warning(err)


# Azure Blob Storage uses a flat scheme, and has no true directory hierarchy. Forward
# slashes are used to delimit a _virtual_ directory structure.
def is_valid_blob_name(blob_name: str) -> bool:
    # https://docs.microsoft.com/en-us/rest/api/storageservices/naming-and-referencing-containers--blobs--and-metadata#blob-names
    MIN_LENGTH = 1
    MAX_LENGTH = 1024  # Inclusive
    MAX_PATH_SEGMENTS = 254

    length = len(blob_name)

    # No leading/trailing whitespace.
    if blob_name != blob_name.strip():
        return False

    if length < MIN_LENGTH:
        return False

    if length > MAX_LENGTH:
        return False

    path = pathlib.PurePosixPath(blob_name)

    if len(path.parts) > MAX_PATH_SEGMENTS:
        return False

    # No path segment should end with a dot (`.`).
    for part in path.parts:
        if part.endswith("."):
            return False

    # Reject absolute paths to avoid confusion.
    if path.is_absolute():
        return False

    # Reject paths with special relative filesystem entries.
    if "." in path.parts:
        return False

    if ".." in path.parts:
        return False

    # Will not have a leading `.`, even if `blob_name` does.
    normalized = path.as_posix()

    return blob_name == normalized


def target_uses_input(config: TaskConfig) -> bool:
    if config.task.target_options is not None:
        for option in config.task.target_options:
            if "{input}" in option:
                return True
    if config.task.target_env is not None:
        for value in config.task.target_env.values():
            if "{input}" in value:
                return True

    return False


def check_config(config: TaskConfig) -> None:
    if config.task.type not in TASK_DEFINITIONS:
        raise TaskConfigError("unsupported task type: %s" % config.task.type.name)

    if config.vm is not None and config.pool is not None:
        raise TaskConfigError("either the vm or pool must be specified, but not both")

    definition = TASK_DEFINITIONS[config.task.type]

    check_containers(definition, config)

    if (
        TaskFeature.supervisor_exe in definition.features
        and not config.task.supervisor_exe
    ):
        err = "missing supervisor_exe"
        LOGGER.error(err)
        raise TaskConfigError("missing supervisor_exe")

    if (
        TaskFeature.target_must_use_input in definition.features
        and not target_uses_input(config)
    ):
        raise TaskConfigError("{input} must be used in target_env or target_options")

    if config.vm:
        err = "specifying task config vm is no longer supported"
        raise TaskConfigError(err)

    if not config.pool:
        raise TaskConfigError("pool must be specified")

    if not check_val(definition.vm.compare, definition.vm.value, config.pool.count):
        err = "invalid vm count: expected %s %d, got %s" % (
            definition.vm.compare,
            definition.vm.value,
            config.pool.count,
        )
        LOGGER.error(err)
        raise TaskConfigError(err)

    pool = Pool.get_by_name(config.pool.pool_name)
    if not isinstance(pool, Pool):
        raise TaskConfigError(f"invalid pool: {config.pool.pool_name}")

    check_target_exe(config, definition)

    if TaskFeature.generator_exe in definition.features:
        container = [x for x in config.containers if x.type == ContainerType.tools][0]
        if not config.task.generator_exe:
            raise TaskConfigError("generator_exe is not defined")

        tools_paths = ["{tools_dir}/", "{tools_dir}\\"]
        for tool_path in tools_paths:
            if config.task.generator_exe.startswith(tool_path):
                generator = config.task.generator_exe.replace(tool_path, "")
                if not blob_exists(container.name, generator, StorageType.corpus):
                    err = (
                        "generator_exe `%s` does not exist in the tools container `%s`"
                        % (
                            config.task.generator_exe,
                            container.name,
                        )
                    )
                    LOGGER.error(err)
                    raise TaskConfigError(err)

    if TaskFeature.stats_file in definition.features:
        if config.task.stats_file is not None and config.task.stats_format is None:
            err = "using a stats_file requires a stats_format"
            LOGGER.error(err)
            raise TaskConfigError(err)


def build_task_config(job: Job, task: Task) -> TaskUnitConfig:
    job_id = job.job_id
    task_id = task.task_id
    task_config = task.config

    if task_config.task.type not in TASK_DEFINITIONS:
        raise TaskConfigError("unsupported task type: %s" % task_config.task.type.name)

    definition = TASK_DEFINITIONS[task_config.task.type]

    config = TaskUnitConfig(
        job_id=job_id,
        task_id=task_id,
        task_type=task_config.task.type,
        instance_telemetry_key=os.environ.get("APPINSIGHTS_INSTRUMENTATIONKEY"),
        microsoft_telemetry_key=os.environ.get("ONEFUZZ_TELEMETRY"),
        heartbeat_queue=get_queue_sas(
            "task-heartbeat",
            StorageType.config,
            add=True,
        ),
        instance_id=get_instance_id(),
    )

    if job.config.logs:
        config.logs = add_container_sas_url(job.config.logs)
    else:
        LOGGER.warning("Missing log container:  job_id %s, task_id %s", job_id, task_id)

    if definition.monitor_queue:
        config.input_queue = get_queue_sas(
            task_id,
            StorageType.corpus,
            add=True,
            read=True,
            update=True,
            process=True,
        )

    for container_def in definition.containers:
        if container_def.type == ContainerType.setup:
            continue

        containers = []
        for (i, container) in enumerate(task_config.containers):
            if container.type != container_def.type:
                continue

            containers.append(
                {
                    "path": "_".join(["task", container_def.type.name, str(i)]),
                    "url": get_container_sas_url(
                        container.name,
                        StorageType.corpus,
                        read=ContainerPermission.Read in container_def.permissions,
                        write=ContainerPermission.Write in container_def.permissions,
                        delete=ContainerPermission.Delete in container_def.permissions,
                        list_=ContainerPermission.List in container_def.permissions,
                    ),
                }
            )

        if not containers:
            continue

        if (
            container_def.compare in [Compare.Equal, Compare.AtMost]
            and container_def.value == 1
        ):
            setattr(config, container_def.type.name, containers[0])
        else:
            setattr(config, container_def.type.name, containers)

    EMPTY_DICT: Dict[str, str] = {}
    EMPTY_LIST: List[str] = []

    if TaskFeature.supervisor_exe in definition.features:
        config.supervisor_exe = task_config.task.supervisor_exe

    if TaskFeature.supervisor_env in definition.features:
        config.supervisor_env = task_config.task.supervisor_env or EMPTY_DICT

    if TaskFeature.supervisor_options in definition.features:
        config.supervisor_options = task_config.task.supervisor_options or EMPTY_LIST

    if TaskFeature.supervisor_input_marker in definition.features:
        config.supervisor_input_marker = task_config.task.supervisor_input_marker

    if TaskFeature.target_exe in definition.features:
        config.target_exe = task_config.task.target_exe

    if (
        TaskFeature.target_exe_optional in definition.features
        and task_config.task.target_exe
    ):
        config.target_exe = task_config.task.target_exe

    if TaskFeature.target_env in definition.features:
        config.target_env = task_config.task.target_env or EMPTY_DICT

    if TaskFeature.target_options in definition.features:
        config.target_options = task_config.task.target_options or EMPTY_LIST

    if TaskFeature.target_options_merge in definition.features:
        config.target_options_merge = task_config.task.target_options_merge or False

    if TaskFeature.target_workers in definition.features:
        config.target_workers = task_config.task.target_workers

    if TaskFeature.rename_output in definition.features:
        config.rename_output = task_config.task.rename_output or False

    if TaskFeature.generator_exe in definition.features:
        config.generator_exe = task_config.task.generator_exe

    if TaskFeature.generator_env in definition.features:
        config.generator_env = task_config.task.generator_env or EMPTY_DICT

    if TaskFeature.generator_options in definition.features:
        config.generator_options = task_config.task.generator_options or EMPTY_LIST

    if (
        TaskFeature.wait_for_files in definition.features
        and task_config.task.wait_for_files
    ):
        config.wait_for_files = task_config.task.wait_for_files.name

    if TaskFeature.analyzer_exe in definition.features:
        config.analyzer_exe = task_config.task.analyzer_exe

    if TaskFeature.analyzer_options in definition.features:
        config.analyzer_options = task_config.task.analyzer_options or EMPTY_LIST

    if TaskFeature.analyzer_env in definition.features:
        config.analyzer_env = task_config.task.analyzer_env or EMPTY_DICT

    if TaskFeature.stats_file in definition.features:
        config.stats_file = task_config.task.stats_file
        config.stats_format = task_config.task.stats_format

    if TaskFeature.target_timeout in definition.features:
        config.target_timeout = task_config.task.target_timeout

    if TaskFeature.check_asan_log in definition.features:
        config.check_asan_log = task_config.task.check_asan_log

    if TaskFeature.check_debugger in definition.features:
        config.check_debugger = task_config.task.check_debugger

    if TaskFeature.check_retry_count in definition.features:
        config.check_retry_count = task_config.task.check_retry_count or 0

    if TaskFeature.ensemble_sync_delay in definition.features:
        config.ensemble_sync_delay = task_config.task.ensemble_sync_delay

    if TaskFeature.check_fuzzer_help in definition.features:
        config.check_fuzzer_help = (
            task_config.task.check_fuzzer_help
            if task_config.task.check_fuzzer_help is not None
            else True
        )

    if TaskFeature.report_list in definition.features:
        config.report_list = task_config.task.report_list

    if TaskFeature.minimized_stack_depth in definition.features:
        config.minimized_stack_depth = task_config.task.minimized_stack_depth

    if TaskFeature.expect_crash_on_failure in definition.features:
        config.expect_crash_on_failure = (
            task_config.task.expect_crash_on_failure
            if task_config.task.expect_crash_on_failure is not None
            else True
        )

    if TaskFeature.coverage_filter in definition.features:
        coverage_filter = task_config.task.coverage_filter

        if coverage_filter is not None:
            config.coverage_filter = coverage_filter

    if TaskFeature.target_assembly in definition.features:
        config.target_assembly = task_config.task.target_assembly

    if TaskFeature.target_class in definition.features:
        config.target_class = task_config.task.target_class

    if TaskFeature.target_method in definition.features:
        config.target_method = task_config.task.target_method

    return config


def get_setup_container(config: TaskConfig) -> Container:
    for container in config.containers:
        if container.type == ContainerType.setup:
            return container.name

    raise TaskConfigError(
        "task missing setup container: task_type = %s" % config.task.type
    )


class TaskConfigError(Exception):
    pass

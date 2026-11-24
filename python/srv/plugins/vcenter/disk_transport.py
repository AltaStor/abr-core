# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""How abr-vmdk-tool reaches vSphere disks: over the VDDK or the vSphere SOAP API, and which VDDK library.

Each value is resolved per field. The transport comes from a backup job's override, else the inventory's, else
the server-wide default. The VDDK library comes from the inventory, else the server-wide setting, else the VDDK
directory: ABR_VMDK_TOOL_VDDK_DIR in the service environment, or the vddk folder next to abr-vmdk-tool, where the
launcher looks by itself. A session records what it resolved when it was created and starts every abr-vmdk-tool
with exactly one of ABR_VMDK_TOOL_VDDK_LIB and ABR_VMDK_TOOL_VDDK_DIR, or neither over SOAP.
"""

from __future__ import annotations

import logging
import os
import re
import struct
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from ...core.mongo_db_wrapper import load_system_state, persist_system_state
from ...core.storage_tool import (
    DISK_TOOL_LAUNCHER,
    default_disk_tool_path,
    disk_tool_has_soap_api,
)
from ...core.utils import utc_now

logger = logging.getLogger(__name__)

VDDK = "vddk"
SOAP = "soap"
TRANSPORTS = (VDDK, SOAP)
FROM_INVENTORY = "inventory"
INHERIT = "inherit"
TRANSPORT_NAMES = {VDDK: "VDDK", SOAP: "the vSphere SOAP API"}
VDDK_LIBRARY = Path("lib64") / "libvixDiskLib.so"
VDDK_INIT_SYMBOL = "VixDiskLib_InitEx"
LIBRARY_VARIABLE = "ABR_VMDK_TOOL_VDDK_LIB"
DIRECTORY_VARIABLE = "ABR_VMDK_TOOL_VDDK_DIR"
SETTINGS_ID = "vsphere_access"
API_DEFAULT_VARIABLE = "ABR_VSPHERE_API"
LIBRARY_DEFAULT_VARIABLE = "ABR_VSPHERE_VDDK_LIB"
OVDDK = "ovddk"
VMWARE_VDDK = "vmware_vddk"
SYSTEM_LIBRARY = "system"
SYSTEM_LIBRARY_DIRS = (Path("/usr/local/lib"), Path("/usr/lib64"), Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib"))
LIB64_NOTE = (
    "A VMware VDDK library must stay in the lib64 folder of its bundle: its transport plugins are found only "
    "through that folder"
)

# What a session records about how it reaches the disks.
SESSION_KEYS = (
    "vsphere_transport",
    "vsphere_api_source",
    "vddk_library",
    "vddk_library_source",
    "vddk_library_file",
    "vddk_directory",
    "vddk_variable",
)

# Where a resolved value comes from. (FROM_INVENTORY is ABR-495's job value "use the inventory's transport".)
FROM_JOB = "job"
FROM_HYPERVISOR = "hypervisor"
FROM_GLOBAL = "global"
FROM_DIRECTORY = "vddk_directory"
# Where the VDDK directory comes from.
DIRECTORY_FROM_ENVIRONMENT = "environment"
DIRECTORY_DEFAULT = "default"

SOURCE_NAMES = {
    FROM_JOB: "set for this job",
    FROM_HYPERVISOR: "set for the hypervisor",
    FROM_GLOBAL: "the server-wide default",
    FROM_DIRECTORY: "from the VDDK directory",
}


def parse_transport(value: Any, *, inventory_allowed: bool = False) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    allowed = (*TRANSPORTS, FROM_INVENTORY) if inventory_allowed else TRANSPORTS
    if text not in allowed:
        raise ValueError(f"transport must be one of: {', '.join(allowed)}")
    return None if text == FROM_INVENTORY else text


def parse_api(value: Any, *, field: str = "api", inherit_allowed: bool = False) -> str | None:
    """vddk or soap; None for inherit (where allowed) and an empty value."""
    text = str(value or "").strip().lower()
    if not text or (inherit_allowed and text == INHERIT):
        return None
    if text not in TRANSPORTS:
        allowed = (*TRANSPORTS, INHERIT) if inherit_allowed else TRANSPORTS
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
    return text


def _metadata(record: dict[str, Any] | None) -> dict[str, Any]:
    metadata = (record or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def inventory_transport_setting(record: dict[str, Any] | None) -> str | None:
    """The inventory's own transport, or None when it uses the server-wide default."""
    value = str(_metadata(record).get("vsphere_transport") or "")
    return value if value in TRANSPORTS else None


def inventory_library_setting(record: dict[str, Any] | None) -> str | None:
    """The inventory's own VDDK library, or None when it uses the server-wide one."""
    return str(_metadata(record).get("vddk_library") or "").strip() or None


def job_transport(job: dict[str, Any] | None) -> str | None:
    parameters = (job or {}).get("task_parameters")
    value = str((parameters if isinstance(parameters, dict) else {}).get("vsphere_transport") or "")
    return value if value in TRANSPORTS else None


def global_settings() -> dict[str, Any]:
    """The server-wide API and VDDK library. Nothing stored means the VDDK and the VDDK directory."""
    stored = load_system_state(SETTINGS_ID) or {}
    api = str(stored.get("api") or stored.get("transport") or "")
    return {
        "api": api if api in TRANSPORTS else VDDK,
        "vddk_library": str(stored.get("vddk_library") or "").strip() or None,
        "updated_at": stored.get("updated_at"),
        "updated_by": stored.get("updated_by"),
    }


def store_global_settings(api: str, vddk_library: str | None, *, updated_by: str | None) -> None:
    persist_system_state(
        {
            "id": SETTINGS_ID,
            "api": api,
            "vddk_library": vddk_library,
            "updated_at": utc_now(),
            "updated_by": updated_by,
        }
    )


def global_settings_stored() -> bool:
    return load_system_state(SETTINGS_ID) is not None


def inventory_transport(record: dict[str, Any] | None, settings: dict[str, Any] | None = None) -> str:
    return inventory_transport_setting(record) or (settings or global_settings())["api"]


def session_transport(job: dict[str, Any] | None, inventory: dict[str, Any] | None) -> str:
    return job_transport(job) or inventory_transport(inventory)


def _launcher(tool_path: Path | None) -> Path:
    return (tool_path or default_disk_tool_path()).expanduser()


def default_vddk_roots(tool_path: Path | None = None) -> list[Path]:
    """Where the launcher looks when ABR_VMDK_TOOL_VDDK_DIR is not set: ../vddk, then ../../vddk."""
    launcher = _launcher(tool_path)
    return [launcher.parent.parent / "vddk", launcher.parent.parent.parent / "vddk"]


def ovddk_library(tool_path: Path | None = None) -> Path:
    return _launcher(tool_path).parent.parent / OVDDK / VDDK_LIBRARY


def detected_libraries(tool_path: Path | None = None) -> list[dict[str, Any]]:
    candidates = [(ovddk_library(tool_path), OVDDK)]
    candidates += [(root / VDDK_LIBRARY, VMWARE_VDDK) for root in (vddk_directory(tool_path)[0], *default_vddk_roots(tool_path))]
    candidates += [(folder / VDDK_LIBRARY.name, SYSTEM_LIBRARY) for folder in SYSTEM_LIBRARY_DIRS]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, kind in candidates:
        real = os.path.realpath(path)
        if real in seen or library_problem(str(path)):
            continue
        seen.add(real)
        found.append({"path": str(path), "kind": kind, "file": resolved_file_name(path)})
    return found


def vddk_directory(tool_path: Path | None = None) -> tuple[Path, str]:
    """The VDDK directory in effect, and whether it is configured in the environment or the default."""
    configured = os.environ.get(DIRECTORY_VARIABLE, "").strip()
    if configured:
        return Path(configured).expanduser(), DIRECTORY_FROM_ENVIRONMENT
    roots = default_vddk_roots(tool_path)
    return next((root for root in roots if (root / VDDK_LIBRARY).is_file()), roots[0]), DIRECTORY_DEFAULT


def default_library(tool_path: Path | None = None) -> Path:
    """The library the VDDK directory in effect gives, which a library left unset follows."""
    return vddk_directory(tool_path)[0] / VDDK_LIBRARY


def vddk_library(tool_path: Path | None = None) -> Path | None:
    library = default_library(tool_path)
    return library if library.is_file() else None


def is_default_library(value: str | None, tool_path: Path | None = None) -> bool:
    if not value:
        return True
    return os.path.normpath(value) == os.path.normpath(str(default_library(tool_path)))


def resolved_file_name(value: str | Path | None) -> str | None:
    """The file a library path ends at after its links, e.g. libvixDiskLib.so.9.1.0.0, or None when there is none."""
    if not value:
        return None
    try:
        real = Path(os.path.realpath(value))
        return real.name if real.is_file() else None
    except OSError:
        return None


_SHT_DYNSYM = 11
_SHN_UNDEF = 0


def _read_at(handle: BinaryIO, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("truncated")
    return data


def exports_symbol(path: str | Path, symbol: str) -> bool | None:
    """Whether an ELF shared library defines ``symbol`` in its dynamic symbol table.

    None when the file is not an ELF file with section headers that this can read. The library is read, never
    loaded: loading a VDDK into the server process would run its initialisers there.
    """
    wanted = symbol.encode("ascii")
    try:
        with open(path, "rb") as handle:
            ident = handle.read(16)
            if len(ident) < 16 or ident[:4] != b"\x7fELF" or ident[4] not in (1, 2) or ident[5] not in (1, 2):
                return None
            wide = ident[4] == 2
            order = "<" if ident[5] == 1 else ">"
            if wide:
                header = _read_at(handle, 0x28, 0x40 - 0x28)
                section_offset, = struct.unpack(order + "Q", header[0:8])
                entry_size, count, _names = struct.unpack(order + "HHH", header[18:24])
                section_format, symbol_format = order + "IIQQQQIIQQ", order + "IBBHQQ"
            else:
                header = _read_at(handle, 0x20, 0x34 - 0x20)
                section_offset, = struct.unpack(order + "I", header[0:4])
                entry_size, count, _names = struct.unpack(order + "HHH", header[14:20])
                section_format, symbol_format = order + "IIIIIIIIII", order + "IIIBBH"
            if not section_offset or not count or entry_size < struct.calcsize(section_format):
                return None
            sections = [
                struct.unpack(section_format, _read_at(handle, section_offset + index * entry_size, struct.calcsize(section_format)))
                for index in range(count)
            ]
            for section in sections:
                kind, offset, size, link, symbol_size = section[1], section[4], section[5], section[6], section[9]
                if kind != _SHT_DYNSYM or not symbol_size or link >= len(sections):
                    continue
                strings = sections[link]
                names = _read_at(handle, strings[4], strings[5])
                table = _read_at(handle, offset, size)
                for start in range(0, size - symbol_size + 1, symbol_size):
                    fields = struct.unpack(symbol_format, table[start:start + struct.calcsize(symbol_format)])
                    name_offset = fields[0]
                    index = fields[3] if wide else fields[5]
                    end = names.find(b"\0", name_offset)
                    if index != _SHN_UNDEF and names[name_offset:end if end >= 0 else None] == wanted:
                        return True
                return False
            return None
    except (OSError, ValueError, struct.error):
        return None


def library_problem(value: str) -> str | None:
    """Why a VDDK library file cannot be used, checked on this server, which is where abr-vmdk-tool runs."""
    path = Path(value)
    if not path.is_absolute():
        return f"The VDDK library must be a full path that starts with /, or ovddk for the oVDDK library of the backup server, not {value}"
    if not path.exists():
        return f"There is no file {value} on the backup server"
    if not path.is_file():
        return f"{value} is not a file"
    if not os.access(path, os.R_OK):
        return f"{value} cannot be read by the backup service"
    exports = exports_symbol(path, VDDK_INIT_SYMBOL)
    if exports is None:
        return f"{value} is not a shared library"
    if not exports:
        return f"{value} does not export '{VDDK_INIT_SYMBOL}', so it is not a VDDK library"
    return None


def disk_tool_takes_library_file(tool_path: Path | None = None) -> bool:
    """Whether the launcher knows ABR_VMDK_TOOL_VDDK_LIB; an older one would ignore it and load another library."""
    launcher = _launcher(tool_path)
    if launcher.name != DISK_TOOL_LAUNCHER:
        return True
    try:
        return LIBRARY_VARIABLE in launcher.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def library_unsupported_message() -> str:
    return (
        f"abr-vmdk-tool on this server does not take a VDDK library file ({LIBRARY_VARIABLE}). Install a newer "
        "AltaStor Backup build, or leave the VDDK library at its default"
    )


def resolve_library(
    inventory: dict[str, Any] | None, settings: dict[str, Any] | None = None, tool_path: Path | None = None
) -> tuple[str, str]:
    """The VDDK library for an inventory, and where it comes from."""
    own = inventory_library_setting(inventory)
    if own:
        return own, FROM_HYPERVISOR
    configured = (settings or global_settings())["vddk_library"]
    if configured:
        return configured, FROM_GLOBAL
    return str(default_library(tool_path)), FROM_DIRECTORY


def resolve_access(
    inventory: dict[str, Any] | None,
    job: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    tool_path: Path | None = None,
) -> dict[str, Any]:
    settings = settings or global_settings()
    if job_transport(job):
        api, api_source = job_transport(job), FROM_JOB
    elif inventory_transport_setting(inventory):
        api, api_source = inventory_transport_setting(inventory), FROM_HYPERVISOR
    else:
        api, api_source = settings["api"], FROM_GLOBAL
    library, library_source = resolve_library(inventory, settings, tool_path)
    return {
        "api": api,
        "api_source": api_source,
        "vddk_library": library,
        "vddk_library_source": library_source,
        "vddk_library_file": resolved_file_name(library),
    }


def inventory_view(record: dict[str, Any] | None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a vSphere inventory shows: its own values (inherit / None: the server-wide one) and what is in effect.

    ``transport`` is ABR-495's name for the API in effect, kept for the clients that read it.
    """
    access = resolve_access(record, settings=settings or global_settings())
    return {
        "transport": access["api"],
        "api": inventory_transport_setting(record) or INHERIT,
        "effective_api": access["api"],
        "api_source": access["api_source"],
        "vddk_library": inventory_library_setting(record),
        "effective_vddk_library": access["vddk_library"],
        "vddk_library_source": access["vddk_library_source"],
        "vddk_library_file": access["vddk_library_file"],
        "vddk_library_problem": transport_problem(
            VDDK, library=explicit_library(access["vddk_library"], access["vddk_library_source"])
        ),
    }


def explicit_library(library: str | None, source: str | None, tool_path: Path | None = None) -> str | None:
    """The library to hand over as ABR_VMDK_TOOL_VDDK_LIB, or None when the VDDK directory gives it."""
    if source not in (FROM_HYPERVISOR, FROM_GLOBAL) or is_default_library(library, tool_path):
        return None
    return library


def session_parameters(
    inventory: dict[str, Any] | None, job: dict[str, Any] | None = None, tool_path: Path | None = None
) -> dict[str, str]:
    """What a session records when it is created, so a later change never touches it."""
    access = resolve_access(inventory, job, tool_path=tool_path)
    parameters = {"vsphere_transport": access["api"], "vsphere_api_source": access["api_source"]}
    if access["api"] != VDDK:
        return parameters
    parameters["vddk_library"] = access["vddk_library"]
    parameters["vddk_library_source"] = access["vddk_library_source"]
    if access["vddk_library_file"]:
        parameters["vddk_library_file"] = access["vddk_library_file"]
    if explicit_library(access["vddk_library"], access["vddk_library_source"], tool_path):
        parameters["vddk_variable"] = LIBRARY_VARIABLE
        return parameters
    directory, origin = vddk_directory(tool_path)
    # A default folder that is not there is left to the launcher, which then starts without a VDDK, as before.
    if origin == DIRECTORY_FROM_ENVIRONMENT or (directory / VDDK_LIBRARY).is_file():
        parameters["vddk_directory"] = str(directory)
        parameters["vddk_variable"] = DIRECTORY_VARIABLE
    return parameters


def parameters_library(parameters: Mapping[str, Any] | None) -> str | None:
    """The library a session hands over as ABR_VMDK_TOOL_VDDK_LIB, or None."""
    parameters = parameters or {}
    if parameters.get("vsphere_transport") != VDDK or parameters.get("vddk_variable") != LIBRARY_VARIABLE:
        return None
    return str(parameters.get("vddk_library") or "") or None


def tool_environment(parameters: Mapping[str, Any] | None, base: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The environment of an abr-vmdk-tool that reads or writes a vSphere disk for a session.

    None for a session from before these settings, which runs the tool with the service's environment as it did.
    Otherwise exactly one of ABR_VMDK_TOOL_VDDK_LIB and ABR_VMDK_TOOL_VDDK_DIR, whatever the service inherited, or
    neither over SOAP and when there is no VDDK directory: the launcher refuses both together, and the tool needs
    no VDDK over SOAP.
    """
    parameters = parameters or {}
    if not parameters.get("vsphere_api_source"):
        return None
    environment = dict(os.environ if base is None else base)
    environment.pop(LIBRARY_VARIABLE, None)
    environment.pop(DIRECTORY_VARIABLE, None)
    if parameters.get("vsphere_transport") != VDDK:
        return environment
    library = parameters_library(parameters)
    if library:
        environment[LIBRARY_VARIABLE] = library
    elif parameters.get("vddk_variable") == DIRECTORY_VARIABLE and parameters.get("vddk_directory"):
        environment[DIRECTORY_VARIABLE] = str(parameters["vddk_directory"])
    return environment


def describe_tool_environment(parameters: Mapping[str, Any] | None) -> str:
    environment = tool_environment(parameters, base={})
    if environment is None:
        return "the service's own environment"
    if LIBRARY_VARIABLE in environment:
        return f"{LIBRARY_VARIABLE}={environment[LIBRARY_VARIABLE]}"
    if DIRECTORY_VARIABLE in environment:
        return f"{DIRECTORY_VARIABLE}={environment[DIRECTORY_VARIABLE]}"
    return f"neither {LIBRARY_VARIABLE} nor {DIRECTORY_VARIABLE}"


def describe_access(parameters: Mapping[str, Any] | None, *, verb: str) -> str:
    """The session log line that says how the session reaches the disks."""
    parameters = parameters or {}
    transport = str(parameters.get("vsphere_transport") or VDDK)
    text = f"Disks are {verb} over {TRANSPORT_NAMES.get(transport, transport)}"
    source = SOURCE_NAMES.get(str(parameters.get("vsphere_api_source") or ""))
    if source:
        text += f" ({source})"
    library = parameters.get("vddk_library")
    if transport == VDDK and library:
        resolved = parameters.get("vddk_library_file")
        named = f"{library} → {resolved}" if resolved and resolved != Path(str(library)).name else str(library)
        text += f", with the VDDK library {named}"
        source = SOURCE_NAMES.get(str(parameters.get("vddk_library_source") or ""))
        if source:
            text += f" ({source})"
    return text


def transport_problem(transport: str, tool_path: Path | None = None, *, library: str | None = None) -> str | None:
    """Why this server cannot use ``transport`` now, or None. ``library`` is a library file named explicitly."""
    launcher = _launcher(tool_path)
    if launcher.name != DISK_TOOL_LAUNCHER:
        return None
    configured = os.environ.get(DIRECTORY_VARIABLE, "").strip()
    if configured and not (Path(configured).expanduser() / VDDK_LIBRARY).is_file():
        return (
            f"{DIRECTORY_VARIABLE} is set to {configured}, which has no lib64/libvixDiskLib.so, and abr-vmdk-tool "
            "does not start until it points at the VMware VDDK or is unset; neither transport works until then"
        )
    if transport == SOAP and not disk_tool_has_soap_api(launcher):
        return (
            "The vSphere SOAP transport is not available: abr-vmdk-tool on this server has no --api soap. "
            "Install a newer AltaStor Backup build, or use the VDDK transport"
        )
    if transport == VDDK and library and not is_default_library(library, launcher):
        if not disk_tool_takes_library_file(launcher):
            return f"The VDDK transport is not available: {library_unsupported_message()}"
        problem = library_problem(library)
        if problem:
            return f"The VDDK transport is not available: {problem}. Choose another VDDK library, or use the vSphere SOAP transport"
        return None
    if transport == VDDK and vddk_library(launcher) is None:
        return (
            "The VDDK transport is not available: the VMware VDDK is not installed (no lib64/libvixDiskLib.so "
            f"in {DIRECTORY_VARIABLE} or in the vddk folder next to abr-vmdk-tool). Install the VDDK, set a VDDK "
            "library, or use the vSphere SOAP transport"
        )
    return None


def tool_api_args(transport: str | None) -> list[str]:
    return ["--api", SOAP] if transport == SOAP else []


CONNECTION_FAILURES: dict[str, tuple[str, str]] = {
    "VIX_E_HOST_NETWORK_CONN_REFUSED": ("The server refused connection", "The server refused the connection"),
    "VIX_E_CANNOT_CONNECT_TO_HOST": ("Cannot connect to the host", "Connecting to the host failed"),
    "VIX_E_HOST_NOT_CONNECTED": ("Unable to connect to the host", "Not connected to the host"),
    "VIX_E_HOST_CONNECTION_LOST": ("The connection to the host was lost", "Connection to the host lost"),
    "VIX_E_HOST_TCP_CONN_LOST": ("The connection was lost", "The TCP connection was lost"),
    "VIX_E_HOST_TCP_SOCKET_ERROR": ("There was an error in communication", "A TCP socket error occurred"),
    "VIX_E_HOST_SERVER_NOT_FOUND": ("The specified server could not be contacted", "The server was not found"),
    "VIX_E_HOST_SERVER_NOT_AVAILABLE": ("The host is not available", "The server is not available"),
    "VIX_E_HOST_SERVER_SHUTDOWN": ("The server shutdown", "The server is shutting down"),
    "VIX_E_VM_HOST_DISCONNECTED": (
        "Unable to communicate with the virtual machine's host because it is disconnected",
        "The host of the virtual machine is disconnected",
    ),
    "VIX_E_NET_HTTP_COULDNT_CONNECT": ("Failed to connect to host or proxy", "The HTTP connection could not be established"),
    "VIX_E_NET_HTTP_OPERATION_TIMEDOUT": ("Network operation timed out", "The HTTP operation timed out"),
    "VIX_E_NET_HTTP_TRANSFER": ("Failure sending/receiving network data", "The HTTP transfer failed"),
}

CONNECTION_FAILURE = re.compile(
    "|".join(re.escape(text) for texts in CONNECTION_FAILURES.values() for text in texts),
    re.IGNORECASE,
)


def is_connection_failure(text: object) -> bool:
    return bool(CONNECTION_FAILURE.search(str(text or "")))

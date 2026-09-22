# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

import functools
import socket
import hashlib
import logging
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TypeVar

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl
from ...core.server_defaults import env_int
from ..hypervisor import Hypervisor
from .vcenter_disk_types import VddkDiskType, classify_vmware_disk_type, hostagent_disk_type_from_code

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _keepalive_interval_seconds() -> int:
    return env_int("BACKUPSERVER_VCENTER_SESSION_KEEPALIVE_SECONDS")


def _retry_on_expired_session(method: Callable[..., _T]) -> Callable[..., _T]:
    """Re-login and retry once when vCenter has expired our API session.

    vCenter closes an API session that has made no calls for
    ``config.vmacore.soap.sessionTimeout`` (30 minutes out of the box). A
    backup spends nearly all of its wall clock inside the data mover, which
    talks to the host over its own VDDK connection -- so from this SOAP
    session's point of view a large-VM transfer is pure idle time. Every
    inventory lookup below opens with a vim.view.ViewManager container view,
    so an expired session surfaces there first: the vim.fault.NotAuthenticated
    ("The session is not authenticated") that failed snapshot removal at the
    end of a 94GB backup.

    Re-authenticating the existing SOAP stub rather than reconnecting keeps
    every managed object the caller already holds valid, so both the retry and
    whatever the caller does with its result run on a live session.
    """

    @functools.wraps(method)
    def wrapper(self: "VCenterNode", *args: Any, **kwargs: Any) -> _T:
        try:
            return method(self, *args, **kwargs)
        except vim.fault.NotAuthenticated:
            logger.warning(
                "vCenter session for %s expired before %s; re-authenticating and retrying.",
                self.host,
                method.__name__,
            )
            self.reauthenticate()
            return method(self, *args, **kwargs)

    return wrapper


def _normalize_vm_firmware(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"bios", "efi"}:
        return normalized
    return None


def _is_truthy_vmware_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_virtual_disk_device(device: Any) -> bool:
    return isinstance(device, vim.vm.device.VirtualDisk) or type(device).__name__.endswith("Disk")


def _is_scsi_controller_device(device: Any) -> bool:
    return isinstance(device, vim.vm.device.VirtualSCSIController) or (
        hasattr(device, "key") and hasattr(device, "busNumber")
    )


_SCSI_CONTROLLER_TYPE_NAMES: dict[type, str] = {
    vim.vm.device.ParaVirtualSCSIController: "paravirtual",
    vim.vm.device.VirtualLsiLogicSASController: "lsilogicsas",
    vim.vm.device.VirtualLsiLogicController: "lsilogic",
    vim.vm.device.VirtualBusLogicController: "buslogic",
}
_SCSI_CONTROLLER_CLASS_BY_NAME: dict[str, type] = {
    name: cls for cls, name in _SCSI_CONTROLLER_TYPE_NAMES.items()
}
_DEFAULT_SCSI_CONTROLLER_TYPE = "paravirtual"


def _scsi_controller_type_name(device: Any) -> str | None:
    for cls, name in _SCSI_CONTROLLER_TYPE_NAMES.items():
        if isinstance(device, cls):
            return name
    return None


_PLACEHOLDER_DISK_UUIDS = {"n/a", "none", "null", "unknown", ""}


def _normalize_disk_uuid(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return None if normalized.lower() in _PLACEHOLDER_DISK_UUIDS else normalized


def _scsi_controller_class_for_type(type_name: Any) -> type:
    normalized = str(type_name or "").strip().lower()
    return _SCSI_CONTROLLER_CLASS_BY_NAME.get(
        normalized, _SCSI_CONTROLLER_CLASS_BY_NAME[_DEFAULT_SCSI_CONTROLLER_TYPE]
    )


def _fault_message(error: Any) -> str:
    """Best-effort human-readable text for a vCenter task/SOAP fault.

    ``TaskInfo.error`` is normally a ``LocalizedMethodFault`` with a
    ``localizedMessage``, but some faults (and the raw ``vim.fault.*``
    exceptions pyVmomi raises synchronously, e.g. HostNotConnected) only
    carry the plain ``msg`` field. Falling straight to ``str(error)`` in
    those cases dumps the object's full internal repr (dynamicType,
    dynamicProperty, faultCause, ...), which is what leaked into job error
    messages before this fallback chain existed.
    """
    return str(
        getattr(error, "localizedMessage", None)
        or getattr(error, "msg", None)
        or error
    )


class VCenterNode(Hypervisor):
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 443,
        verify_ssl: bool = False,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.verify_ssl = verify_ssl
        self.si: Optional[Any] = None
        # Guards the connection itself (re-login / reconnect), so the keepalive
        # thread below can never swap self.si out from under a caller.
        self._session_lock = threading.RLock()

    def _named_objects(self, object_types: Any, root: Any = None) -> List[tuple]:
        types = list(object_types) if isinstance(object_types, (list, tuple)) else [object_types]
        content = self.si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            root if root is not None else content.rootFolder, types, True
        )
        try:
            collector = content.propertyCollector
            traversal = vmodl.query.PropertyCollector.TraversalSpec(
                name="viewToObject", type=vim.view.ContainerView, path="view", skip=False
            )
            object_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container, skip=True, selectSet=[traversal]
            )
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[object_spec],
                propSet=[
                    vmodl.query.PropertyCollector.PropertySpec(
                        type=object_type, pathSet=["name"], all=False
                    )
                    for object_type in types
                ],
            )
            options = vmodl.query.PropertyCollector.RetrieveOptions()
            found: List[tuple] = []
            batch = collector.RetrievePropertiesEx([filter_spec], options)
            while batch is not None:
                for entry in batch.objects or []:
                    name = next(
                        (prop.val for prop in (entry.propSet or []) if prop.name == "name"),
                        None,
                    )
                    if isinstance(name, str):
                        found.append((entry.obj, name))
                token = getattr(batch, "token", None)
                batch = collector.ContinueRetrievePropertiesEx(token) if token else None
            return found
        finally:
            container.Destroy()

    @_retry_on_expired_session
    def _list_vms(self) -> List[Any]:
        return [vm for vm, name in self._named_objects(vim.VirtualMachine) if not name.startswith("vCLS-")]

    def connect(self) -> None:
        context = ssl.create_default_context() if self.verify_ssl else ssl._create_unverified_context()
        self.si = SmartConnect(
            host=self.host,
            port=self.port,
            user=self.user,
            pwd=self.password,
            sslContext=context,
        )
        logger.info("Connected to vCenter: %s:%s", self.host, self.port)

    def disconnect(self) -> None:
        with self._session_lock:
            if self.si:
                Disconnect(self.si)
                self.si = None
                logger.info("Disconnected from vCenter: %s:%s", self.host, self.port)

    def reauthenticate(self) -> None:
        """Get back to an authenticated session after vCenter expired ours.

        Prefers logging in again on the existing stub: RetrieveServiceContent
        needs no session, and a fresh Login on the same connection revives
        every MoRef the caller is still holding. Only if that fails (the
        connection itself is gone, not just the session) do we reconnect,
        which does invalidate previously fetched objects.
        """
        with self._session_lock:
            if self.si is not None:
                try:
                    self.si.RetrieveContent().sessionManager.Login(self.user, self.password, None)
                    logger.info("Re-authenticated vCenter session: %s:%s", self.host, self.port)
                    return
                except Exception as exc:
                    logger.warning(
                        "Re-login on the existing connection to %s failed (%s); reconnecting.",
                        self.host,
                        exc,
                    )
                    try:
                        Disconnect(self.si)
                    except Exception:
                        logger.debug(
                            "Ignoring failure to drop the stale vCenter connection to %s",
                            self.host,
                            exc_info=True,
                        )
                    self.si = None
            self.connect()

    def touch_session(self) -> None:
        """One cheap authenticated call, to reset vCenter's session idle timer."""
        with self._session_lock:
            if self.si is None:
                return
            try:
                self.si.CurrentTime()
            except vim.fault.NotAuthenticated:
                self.reauthenticate()

    @contextmanager
    def session_keepalive(self, interval_seconds: float | None = None) -> Iterator[None]:
        """Hold this session open across a long operation that doesn't use it.

        A backup or restore hands the actual data transfer to abr-vmdk-tool,
        which uses its own VDDK connection; this SOAP session then sits idle
        for as long as the transfer takes. Past vCenter's session timeout
        (30 minutes by default) it is closed, and the next call the job makes
        -- collecting change IDs, removing the backup snapshot -- fails with
        vim.fault.NotAuthenticated. Pinging from a background thread keeps the
        session the job resumes on alive; _retry_on_expired_session is the
        fallback for when even that does not (vCenter restart, session evicted).
        """
        interval = interval_seconds if interval_seconds is not None else _keepalive_interval_seconds()
        stop = threading.Event()

        def _ping_until_stopped() -> None:
            while not stop.wait(interval):
                try:
                    self.touch_session()
                except Exception:
                    # Never fatal: a ping that cannot get through only costs
                    # the job the head start over _retry_on_expired_session,
                    # which still recovers the session on the next real call.
                    logger.warning(
                        "vCenter session keepalive ping to %s failed.", self.host, exc_info=True
                    )

        thread = threading.Thread(
            target=_ping_until_stopped,
            name=f"vcenter-keepalive-{self.host}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=30)

    def backup(
        self,
        task: dict[str, Any],
        progress_callback=None,
    ) -> dict[str, Any]:
        from .vcenter_backup import run_vcenter_backup

        return run_vcenter_backup(task, progress_callback=progress_callback)

    def restore(
        self,
        task: dict[str, Any],
        progress_callback=None,
    ) -> dict[str, Any]:
        from .vcenter_restore import run_vcenter_restore

        return run_vcenter_restore(task, progress_callback=progress_callback)

    def get_thumbprint(self) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        try:
            with socket.create_connection((self.host, self.port)) as sock:
                with context.wrap_socket(sock, server_hostname=self.host) as ssock:
                    der_cert = ssock.getpeercert(binary_form=True)
                    
                    if not der_cert:
                        return ""
                    
                    thumb_sha1 = hashlib.sha1(der_cert).hexdigest()
                    return ":".join(thumb_sha1[i:i+2] for i in range(0, len(thumb_sha1), 2)).upper()
                    
        except Exception as e:
            logger.error(f"Unable to get certificate from {self.host}: {e}")
            return ""

    def get_service_type(self) -> str:
        if self.si is None:
            raise ValueError("Not connected")
        about = self.si.RetrieveContent().about
        return str(getattr(about, "apiType", "") or "")

    def get_backup_parameters(self, vm_name: str, snapshot_name: str) -> Dict[str, str]:
        vm = self._get_vm(vm_name)        
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found.")

        vm_moref = vm._moId
        snapshot_list = vm.snapshot.rootSnapshotList if vm.snapshot else []
        target_snap = self._find_snapshot(snapshot_list, snapshot_name)
        
        if not target_snap:
            logger.error(
                "Snapshot '%s' is not reachable on VM '%s' (host=%s); cannot read backup parameters.",
                snapshot_name,
                vm_name,
                self.host,
            )
            raise ValueError(
                f"Snapshot '{snapshot_name}' is not reachable on VM '{vm_name}'; aborting backup."
            )

        ss_moref = target_snap.snapshot._moId

        logger.info(
            "Read backup parameters for VM '%s' from snapshot '%s' (vm_moref=%s, snapshot_moref=%s).",
            vm_name,
            snapshot_name,
            vm_moref,
            ss_moref,
        )

        return {
            "vm_moref": vm_moref,
            "snapshot_moref": ss_moref,
        }

    def _wait_for_task(self, task: Any) -> Any:
        pending = (vim.TaskInfo.State.queued, vim.TaskInfo.State.running)
        while task.info.state in pending:
            time.sleep(2)

        if task.info.state == vim.TaskInfo.State.success:
            return task.info.result

        raise RuntimeError(f"vCenter task failed: {_fault_message(task.info.error)}")

    @_retry_on_expired_session
    def _get_vm(self, vm_name: str) -> Optional[Any]:
        for vm, name in self._named_objects(vim.VirtualMachine):
            if name == vm_name:
                return vm
        return None

    def _get_vm_by_moref(self, vm_moref: str) -> Optional[Any]:
        if not vm_moref:
            return None
        for vm in self._list_vms():
            if getattr(vm, "_moId", None) == vm_moref:
                return vm
        return None

    @_retry_on_expired_session
    def _get_host(self, host_name: str) -> Optional[Any]:
        for host, name in self._named_objects(vim.HostSystem):
            if name == host_name:
                return host
        return None

    @_retry_on_expired_session
    def _get_datastore(self, datastore_name: str) -> Optional[Any]:
        for datastore, name in self._named_objects(vim.Datastore):
            if name == datastore_name:
                return datastore
        return None

    def _get_datacenter(self, entity: Any) -> Optional[Any]:
        current = entity
        while current is not None:
            if isinstance(current, vim.Datacenter):
                return current
            current = getattr(current, "parent", None)
        return None

    def _find_network(self, datacenter: Any, host: Any, network_name: str) -> Optional[Any]:
        for network in getattr(host, "network", []) or []:
            if getattr(network, "name", None) == network_name:
                return network

        for network, name in self._named_objects(
            [vim.Network, vim.dvs.DistributedVirtualPortgroup], root=datacenter.networkFolder
        ):
            if name == network_name:
                return network
        return None

    def _build_nic_spec(self, datacenter: Any, host: Any, adapter: dict[str, Any]) -> Optional[Any]:
        network_name = str(adapter.get("network") or "").strip()
        if not network_name:
            return None

        network_obj = self._find_network(datacenter, host, network_name)
        if network_obj is None:
            logger.warning("Network '%s' not found while preparing VM restore", network_name)
            return None

        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add

        nic = vim.vm.device.VirtualVmxnet3()
        nic.key = -1
        nic.addressType = "generated"
        nic.wakeOnLanEnabled = True
        nic.deviceInfo = vim.Description()
        nic.deviceInfo.label = str(adapter.get("label") or network_name)
        nic.deviceInfo.summary = network_name
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        nic.connectable.startConnected = bool(adapter.get("connected", True))
        nic.connectable.connected = bool(adapter.get("connected", True))
        nic.connectable.allowGuestControl = True

        if isinstance(network_obj, vim.dvs.DistributedVirtualPortgroup):
            port_connection = vim.dvs.PortConnection()
            port_connection.portgroupKey = network_obj.key
            dvs = network_obj.config.distributedVirtualSwitch
            port_connection.switchUuid = dvs.uuid
            backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
            backing.port = port_connection
        else:
            backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
            backing.deviceName = network_name
            backing.network = network_obj

        nic.backing = backing
        nic_spec.device = nic
        return nic_spec

    def _find_snapshot(self, snapshot_list: List[Any], name: str) -> Optional[Any]:
        for snapshot in snapshot_list:
            if snapshot.name == name:
                return snapshot
            found = self._find_snapshot(snapshot.childSnapshotList, name)
            if found:
                return found
        return None

    def _collect_disks(self, vm: Any) -> List[Dict[str, Any]]:
        def resolve_source_backing(backing: Any) -> Any:
            current = backing
            seen: set[int] = set()
            while current is not None:
                ident = id(current)
                if ident in seen:
                    break
                seen.add(ident)
                parent = getattr(current, "parent", None)
                parent_file = getattr(parent, "fileName", None) if parent is not None else None
                if not parent_file:
                    break
                current = parent
            return current

        disks = []
        for device in vm.config.hardware.device:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            current_path = str(getattr(device.backing, "fileName", "") or "")
            source_backing = resolve_source_backing(device.backing)
            source_path = str(
                getattr(source_backing, "fileName", "") or getattr(device.backing, "fileName", "")
            )
            disk_type_code, disk_type_name = classify_vmware_disk_type(source_backing, source_path)
            disks.append(
                {
                    "disk_label": device.deviceInfo.label,
                    "capacity_gb": round(device.capacityInKB / (1024 ** 2), 2),
                    "datastore_path": source_path,
                    "current_datastore_path": current_path,
                    "uuid": getattr(device.backing, "uuid", None),
                    "source_disk_type_code": disk_type_code,
                    "source_disk_type_name": disk_type_name,
                }
            )
        return disks

    # vCenter raises this GenericVmConfigFault when a VM's Change Block
    # Tracking metadata has become corrupted (stale/mismatched .ctk files),
    # independent of whether ensure_cbt_enabled() reports CBT as healthy.
    _CBT_CORRUPTION_FAULT = "Invalid change tracker error code"

    def ensure_vm_host_connected(self, vm_name: str) -> None:
        """Raise a clear error if the ESXi host currently running vm_name has
        dropped its connection to vCenter.

        A disconnected host (network blip, management agent restart, PSOD,
        etc.) can't service snapshot/reconfigure calls for the VMs it runs --
        vCenter rejects them synchronously with vim.fault.HostNotConnected,
        whose raw str() is an unreadable dump of the fault's internal
        fields. Checking connection state up front turns that into an
        actionable message before any CBT/snapshot work starts, rather than
        surfacing the raw fault mid-backup.
        """
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        host = getattr(getattr(vm, "runtime", None), "host", None)
        if host is None:
            return

        connection_state = str(getattr(getattr(host, "runtime", None), "connectionState", "") or "").lower()
        if connection_state and connection_state != "connected":
            try:
                host_name = getattr(host, "name", None)
            except Exception:
                host_name = None
            raise RuntimeError(
                f"Cannot start backup for VM '{vm_name}': its ESXi host "
                f"'{host_name or 'unknown'}' is {connection_state} in vCenter, not connected. "
                "Reconnect the host in vCenter (or wait for it to reconnect) and retry the backup."
            )

    def create_backup_snapshot(self, vm_name: str) -> str:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        try:
            return self._create_snapshot(vm, vm_name)
        except RuntimeError as exc:
            if self._CBT_CORRUPTION_FAULT not in str(exc):
                raise
            logger.warning(
                "VM '%s' hit a corrupted Change Block Tracking state ('%s'); "
                "resetting CBT and retrying the snapshot once.",
                vm_name,
                self._CBT_CORRUPTION_FAULT,
            )
            self._reset_change_tracking(vm, vm_name)
            try:
                return self._create_snapshot(vm, vm_name)
            except RuntimeError as retry_exc:
                if self._CBT_CORRUPTION_FAULT not in str(retry_exc):
                    raise
                raise RuntimeError(
                    f"Snapshot creation for VM '{vm_name}' still fails with a "
                    "corrupted Change Block Tracking state after an automatic "
                    "CBT reset. This VM likely needs a power cycle to fully "
                    "clear its .ctk files (see VMware KB 2048201) before "
                    f"backups can succeed again. Original error: {retry_exc}"
                ) from retry_exc

    def _create_snapshot(self, vm: Any, vm_name: str) -> str:
        snapshot_name = f"FullBackup_{int(time.time())}"
        logger.info("Creating snapshot '%s' for VM '%s'...", snapshot_name, vm_name)

        task = vm.CreateSnapshot_Task(
            name=snapshot_name,
            description="Snapshot for full backup.",
            memory=False,
            quiesce=True,
        )
        self._wait_for_task(task)
        logger.info("Snapshot '%s' created.", snapshot_name)
        return snapshot_name

    def _reset_change_tracking(self, vm: Any, vm_name: str) -> None:
        """Recover from a corrupted CBT state without powering off the VM.

        Per VMware KB 2048201, toggling Change Block Tracking off then back
        on -- with a throwaway snapshot taken and removed while it's off --
        clears stale/mismatched .ctk files. The snapshot retried by the
        caller afterwards regenerates fresh, valid .ctk files under a new
        CBT generation.
        """
        logger.info("Disabling CBT for VM '%s' to clear corrupted tracker state.", vm_name)
        disable_task = vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(changeTrackingEnabled=False))
        self._wait_for_task(disable_task)

        flush_name = f"CBTReset_{int(time.time())}"
        flush_task = vm.CreateSnapshot_Task(
            name=flush_name,
            description="Temporary snapshot to reset Change Block Tracking.",
            memory=False,
            quiesce=False,
        )
        self._wait_for_task(flush_task)
        self.remove_snapshot(vm_name, flush_name)

        logger.info("Re-enabling CBT for VM '%s' after reset.", vm_name)
        self.ensure_cbt_enabled(vm_name)

    def ensure_cbt_enabled(self, vm_name: str) -> bool:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        config = getattr(vm, "config", None)
        if config is None:
            raise ValueError(f"VM '{vm_name}' does not expose configuration details.")

        controllers_by_key: dict[int, Any] = {}
        for device in getattr(getattr(config, "hardware", None), "device", []) or []:
            if _is_scsi_controller_device(device):
                controllers_by_key[int(device.key)] = device

        extra_config_values = {
            str(opt.key): getattr(opt, "value", None)
            for opt in (getattr(config, "extraConfig", None) or [])
        }
        global_cbt_enabled = bool(getattr(config, "changeTrackingEnabled", False))
        global_extra_enabled = _is_truthy_vmware_flag(extra_config_values.get("ctkEnabled"))
        extra_config: list[Any] = []
        missing_disk_keys: list[str] = []
        for device in getattr(getattr(config, "hardware", None), "device", []) or []:
            if not _is_virtual_disk_device(device):
                continue
            controller = controllers_by_key.get(int(device.controllerKey))
            bus_number = getattr(controller, "busNumber", None) if controller is not None else None
            unit_number = getattr(device, "unitNumber", None)
            if bus_number is None or unit_number is None:
                continue
            key = f"scsi{int(bus_number)}:{int(unit_number)}.ctkEnabled"
            if _is_truthy_vmware_flag(extra_config_values.get(key)):
                continue
            missing_disk_keys.append(key)
            extra_config.append(
                vim.option.OptionValue(
                    key=key,
                    value="true",
                )
            )

        if global_cbt_enabled and global_extra_enabled and not missing_disk_keys:
            logger.info("CBT is already enabled for VM '%s'.", vm_name)
            return False

        logger.info(
            "Enabling CBT for VM '%s' (global_enabled=%s, global_extra=%s, missing_disk_keys=%s).",
            vm_name,
            global_cbt_enabled,
            global_extra_enabled,
            missing_disk_keys,
        )
        config_spec = vim.vm.ConfigSpec(
            changeTrackingEnabled=True,
            extraConfig=[
                vim.option.OptionValue(key="ctkEnabled", value="true"),
                *extra_config,
            ],
        )
        task = vm.ReconfigVM_Task(spec=config_spec)
        self._wait_for_task(task)
        logger.info("CBT enabled for VM '%s'.", vm_name)
        return True

    def get_snapshot_change_ids(self, vm_name: str, snapshot_name: str) -> List[Dict[str, Any]]:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        snapshot_list = vm.snapshot.rootSnapshotList if vm.snapshot else []
        target_snap = self._find_snapshot(snapshot_list, snapshot_name)
        if not target_snap:
            raise ValueError(f"Snapshot '{snapshot_name}' not found.")

        snapshot_ref = getattr(target_snap, "snapshot", None)
        snapshot_config = getattr(snapshot_ref, "config", None) or getattr(target_snap, "config", None)
        snapshot_hardware = getattr(snapshot_config, "hardware", None)
        if snapshot_hardware is None:
            raise ValueError(f"Snapshot '{snapshot_name}' does not expose hardware details.")

        change_ids: list[dict[str, Any]] = []
        for device in getattr(snapshot_hardware, "device", []) or []:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            backing = getattr(device, "backing", None)
            change_ids.append(
                {
                    "disk_label": getattr(getattr(device, "deviceInfo", None), "label", ""),
                    "datastore_path": str(getattr(backing, "fileName", "") or ""),
                    "change_id": str(getattr(backing, "changeId", "") or ""),
                    "uuid": getattr(backing, "uuid", None),
                    "controller_key": getattr(device, "controllerKey", None),
                    "unit_number": getattr(device, "unitNumber", None),
                    "key": getattr(device, "key", None),
                    "capacity_bytes": int(getattr(device, "capacityInKB", 0) or 0) * 1024,
                }
            )
        return change_ids

    def query_changed_disk_areas(
        self,
        vm_name: str,
        snapshot_name: str,
        disk_key: int,
        change_id: str,
    ) -> List[Dict[str, int]]:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        snapshot_list = vm.snapshot.rootSnapshotList if vm.snapshot else []
        target_snap = self._find_snapshot(snapshot_list, snapshot_name)
        if not target_snap:
            raise ValueError(f"Snapshot '{snapshot_name}' not found.")

        snapshot_ref = getattr(target_snap, "snapshot", None)
        if snapshot_ref is None:
            raise ValueError(f"Snapshot '{snapshot_name}' does not expose a snapshot reference.")

        disk_capacity = 0
        for device in getattr(getattr(vm.config, "hardware", None), "device", []) or []:
            if isinstance(device, vim.vm.device.VirtualDisk) and int(getattr(device, "key", -1)) == int(disk_key):
                disk_capacity = int(getattr(device, "capacityInKB", 0) or 0) * 1024
                break

        changed_areas: list[dict[str, int]] = []
        start_offset = 0
        while True:
            change_info = vm.QueryChangedDiskAreas(
                snapshot=snapshot_ref,
                deviceKey=int(disk_key),
                startOffset=start_offset,
                changeId=change_id,
            )
            for area in getattr(change_info, "changedArea", []) or []:
                changed_areas.append(
                    {
                        "offset": int(getattr(area, "start", 0) or 0),
                        "length": int(getattr(area, "length", 0) or 0),
                    }
                )

            covered_start = int(getattr(change_info, "startOffset", start_offset) or start_offset)
            covered_length = int(getattr(change_info, "length", 0) or 0)
            next_offset = covered_start + covered_length
            if covered_length <= 0 or next_offset <= start_offset:
                break
            if disk_capacity and next_offset >= disk_capacity:
                break
            start_offset = next_offset

        logger.info(
            "Collected %d changed area(s) for VM '%s' disk key %s from changeId %s.",
            len(changed_areas),
            vm_name,
            disk_key,
            change_id,
        )
        return changed_areas

    def get_vm_disks(self, vm_name: str) -> List[Dict[str, Any]]:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        disks = self._collect_disks(vm)
        logger.info("Collected %d disk(s) for VM '%s'.", len(disks), vm_name)
        return disks

    def get_vm_restore_metadata(self, vm_name: str) -> Dict[str, Any]:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        summary = vm.summary
        config = vm.config
        runtime = summary.runtime
        host = getattr(runtime, "host", None)
        firmware = _normalize_vm_firmware(getattr(config, "firmware", None))
        if firmware is None and getattr(config, "firmware", None) not in (None, ""):
            logger.warning(
                "VM '%s' reported unsupported firmware value %r; excluding it from restore metadata.",
                vm_name,
                getattr(config, "firmware", None),
            )

        adapters: list[dict[str, Any]] = []
        # Track the lowest-bus-number SCSI controller: create_vm_shell recreates
        # exactly one SCSI controller (busNumber 0) and attaches every disk to
        # it, so that is the one controller type worth preserving.
        scsi_controller_type: str | None = None
        scsi_controller_bus_number: int | None = None
        for device in config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                backing = getattr(device, "backing", None)
                network_name = getattr(backing, "deviceName", None)
                if network_name is None:
                    port = getattr(backing, "port", None)
                    if port is not None:
                        portgroup_key = getattr(port, "portgroupKey", None)
                        if portgroup_key:
                            network_name = getattr(getattr(device, "deviceInfo", None), "summary", None)
                adapters.append(
                    {
                        "label": getattr(device.deviceInfo, "label", "Network adapter"),
                        "network": str(network_name) if network_name is not None else "",
                        "connected": bool(getattr(device.connectable, "startConnected", True)),
                    }
                )
                continue
            controller_type_name = _scsi_controller_type_name(device)
            if controller_type_name is not None:
                bus_number = getattr(device, "busNumber", None)
                if scsi_controller_bus_number is None or (
                    bus_number is not None and bus_number < scsi_controller_bus_number
                ):
                    scsi_controller_type = controller_type_name
                    scsi_controller_bus_number = bus_number

        boot_options = getattr(config, "bootOptions", None)
        raw_secure_boot = getattr(boot_options, "efiSecureBootEnabled", None)
        secure_boot_enabled = bool(raw_secure_boot) if raw_secure_boot is not None else None

        return {
            "name": summary.config.name,
            "instance_uuid": getattr(config, "instanceUuid", None),
            "guest_id": getattr(config, "guestId", None) or "otherGuest64",
            "firmware": firmware,
            "secure_boot_enabled": secure_boot_enabled,
            "scsi_controller_type": scsi_controller_type,
            "guest_os": summary.config.guestFullName,
            "num_cpu": summary.config.numCpu,
            "memory_mb": summary.config.memorySizeMB,
            "power_state": str(summary.runtime.powerState),
            "source_host": getattr(host, "name", None),
            "network_adapters": adapters,
        }

    def list_hosts(self) -> List[Dict[str, Any]]:
        content = self.si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            items: list[dict[str, Any]] = []
            for host in container.view:
                datacenter = self._get_datacenter(host)
                datastore_names = sorted(
                    str(ds.name)
                    for ds in (getattr(host, "datastore", None) or [])
                    if getattr(ds, "name", None)
                )
                # Capacity/free space, keyed by name alongside `datastores`
                # rather than folded into it, so existing consumers matching
                # on plain datastore-name strings keep working untouched.
                datastore_capacity: dict[str, dict[str, Optional[int]]] = {}
                for ds in getattr(host, "datastore", None) or []:
                    name = getattr(ds, "name", None)
                    if not name:
                        continue
                    summary = getattr(ds, "summary", None)
                    capacity = getattr(summary, "capacity", None)
                    free = getattr(summary, "freeSpace", None)
                    if capacity is None and free is None:
                        continue
                    datastore_capacity[str(name)] = {
                        "capacity_bytes": int(capacity) if capacity is not None else None,
                        "free_bytes": int(free) if free is not None else None,
                    }
                # The inventory tree groups hosts by cluster the way vSphere
                # does; a standalone host has no cluster and sits directly
                # under its datacenter.
                cluster = self._host_cluster(host)
                items.append(
                    {
                        # ``name`` is vCenter's own name for the host and stays the
                        # identity everywhere (restore resolves targets by it), so
                        # the friendly DNS name is carried separately for display.
                        "name": host.name,
                        "dns_name": self._host_dns_name(host),
                        "datacenter": getattr(datacenter, "name", None),
                        "cluster": self._cluster_label(cluster) if cluster is not None else None,
                        "datastores": datastore_names,
                        "datastore_capacity": datastore_capacity,
                        "networks": self._host_network_names(host),
                    }
                )
            items.sort(key=lambda item: item["name"])
            return items
        finally:
            container.Destroy()

    @staticmethod
    def _host_cluster(host: Any) -> Optional[Any]:
        """Return the ClusterComputeResource a host belongs to, else None.

        A host that is part of a cluster has a ``vim.ClusterComputeResource`` as
        its direct parent; a standalone host's parent is a plain
        ``vim.ComputeResource``.
        """
        parent = getattr(host, "parent", None)
        if isinstance(parent, vim.ClusterComputeResource):
            return parent
        return None

    @staticmethod
    def _host_network_names(host: Any) -> List[str]:
        """Port groups reachable from a host, across all of its virtual switches.

        A port group lives on one host's vSwitch (standard switches are per-host),
        so this is what scopes a restore's network mapping: without it the wizard
        offers every port group in the inventory, including ones the chosen host
        has no switch for. Returns [] when the host can't be queried — callers
        read an empty list as "scope unknown" and fall back to offering all.
        """
        try:
            return sorted(
                {
                    str(network.name)
                    for network in (getattr(host, "network", None) or [])
                    if getattr(network, "name", None)
                }
            )
        except Exception:  # unreachable host / bare MoRef — not worth failing discovery
            return []

    @staticmethod
    def _host_dns_name(host: Any) -> Optional[str]:
        """The host's configured DNS name, for display only.

        Hosts added to vCenter by address are named after their IP, which is all
        the inventory tree can otherwise show. Returns None when the name is
        absent or a placeholder, so callers fall back to vCenter's own name.
        """
        try:
            name = host.config.network.dnsConfig.hostName
        except Exception:  # unreachable host / bare MoRef — display is not worth failing on
            return None
        name = str(name or "").strip()
        return name or None if name.lower() not in {"localhost", "unknown"} else None

    @staticmethod
    def _cluster_label(cluster: Any) -> str:
        # ``name`` is a server-backed property; on a bare MoRef (or if the
        # session has gone away) reading it can raise -- fall back to a generic
        # label so callers always get a usable string.
        try:
            name = getattr(cluster, "name", None)
        except Exception:
            name = None
        return str(name) if name else "cluster"

    def clustered_host_name(self) -> Optional[str]:
        """Return the name of the cluster the endpoint's host belongs to.

        Walks every ``HostSystem`` visible on this connection and returns the
        cluster name of the first one whose parent is a
        ``vim.ClusterComputeResource``; returns ``None`` if no host is part of a
        cluster. Callers that only want to reject *standalone ESXi hosts* added
        to a cluster should gate this on ``get_service_type() == "HostAgent"`` --
        a vCenter endpoint is the supported way to back up clustered hosts.
        """
        if self.si is None:
            raise ValueError("Not connected")
        content = self.si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            for host in container.view:
                cluster = self._host_cluster(host)
                if cluster is not None:
                    return self._cluster_label(cluster)
            return None
        finally:
            container.Destroy()

    def create_vm_shell(
        self,
        *,
        vm_name: str,
        host_name: str,
        datastore_name: str,
        config_metadata: Dict[str, Any],
        disk_layout: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        host = self._get_host(host_name)
        if host is None:
            raise ValueError(f"Host '{host_name}' not found on {self.host}.")

        if self._get_vm(vm_name):
            raise ValueError(f"VM '{vm_name}' already exists on {self.host}.")

        datacenter = self._get_datacenter(host)
        if datacenter is None:
            raise ValueError(f"Unable to determine datacenter for host '{host_name}'.")

        def _datastore_on_host(name: str) -> Any:
            for candidate in getattr(host, "datastore", []) or []:
                if getattr(candidate, "name", None) == name:
                    return candidate
            raise ValueError(f"Datastore '{name}' is not accessible from host '{host_name}'.")

        datastore = _datastore_on_host(datastore_name)

        compute_resource = getattr(host, "parent", None)
        resource_pool = getattr(compute_resource, "resourcePool", None)
        if resource_pool is None:
            raise ValueError(f"Host '{host_name}' does not expose a resource pool.")

        requested_scsi_controller_type = str(
            config_metadata.get("scsi_controller_type") or _DEFAULT_SCSI_CONTROLLER_TYPE
        ).strip().lower()
        scsi_controller_class = _scsi_controller_class_for_type(requested_scsi_controller_type)

        scsi_spec = vim.vm.device.VirtualDeviceSpec()
        scsi_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        scsi_controller = scsi_controller_class()
        scsi_controller.key = 1000
        scsi_controller.busNumber = 0
        scsi_controller.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
        scsi_spec.device = scsi_controller

        device_changes: list[Any] = [scsi_spec]
        for adapter in config_metadata.get("network_adapters", []):
            if not isinstance(adapter, dict):
                continue
            nic_spec = self._build_nic_spec(datacenter, host, adapter)
            if nic_spec is not None:
                device_changes.append(nic_spec)

        created_disk_paths: list[str] = []
        disk_layout = disk_layout or []
        for index, disk in enumerate(disk_layout, start=1):
            unit_number = index - 1
            if unit_number >= 7:
                unit_number += 1

            capacity_gb = float(disk.get("capacity_gb") or 1)
            disk_name = str(disk.get("file_name") or f"{index:02d}-disk.vmdk")
            disk_datastore_name = str(disk.get("datastore_name") or "").strip() or datastore_name
            disk_datastore = datastore if disk_datastore_name == datastore_name else _datastore_on_host(disk_datastore_name)
            disk_path = f"[{disk_datastore_name}] {_sanitize_path_segment(vm_name)}/{disk_name}"

            disk_spec = vim.vm.device.VirtualDeviceSpec()
            disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create

            virtual_disk = vim.vm.device.VirtualDisk()
            virtual_disk.key = -1000 - index
            virtual_disk.controllerKey = scsi_controller.key
            virtual_disk.unitNumber = unit_number
            virtual_disk.capacityInKB = int(capacity_gb * 1024 * 1024)
            virtual_disk.deviceInfo = vim.Description()
            virtual_disk.deviceInfo.label = str(disk.get("label") or f"Hard disk {index}")
            virtual_disk.deviceInfo.summary = disk_path

            backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            backing.fileName = disk_path
            backing.datastore = disk_datastore
            backing.diskMode = "persistent"
            backing.thinProvisioned = True
            source_disk_uuid = _normalize_disk_uuid(disk.get("disk_uuid"))
            if source_disk_uuid:
                backing.uuid = source_disk_uuid
            virtual_disk.backing = backing

            disk_spec.device = virtual_disk
            device_changes.append(disk_spec)
            created_disk_paths.append(disk_path)

        vm_path = f"[{datastore_name}]"
        source_firmware = config_metadata.get("firmware")
        requested_firmware = _normalize_vm_firmware(source_firmware)
        if requested_firmware is None:
            normalized_source = str(source_firmware or "").strip()
            if normalized_source:
                raise ValueError(
                    f"Unsupported VM firmware value in restore metadata for '{vm_name}': {source_firmware!r}"
                )
            requested_firmware = "bios"
            logger.warning(
                "Source firmware is missing from restore point metadata; falling back to default firmware '%s'.",
                requested_firmware,
            )
        logger.info(
            "Creating VM shell '%s' on host '%s' with source firmware '%s' and requested firmware '%s'.",
            vm_name,
            host_name,
            _normalize_vm_firmware(source_firmware) or "missing",
            requested_firmware,
        )
        config_spec = vim.vm.ConfigSpec(
            name=vm_name,
            guestId=str(config_metadata.get("guest_id") or "otherGuest64"),
            memoryMB=int(config_metadata.get("memory_mb") or 1024),
            numCPUs=int(config_metadata.get("num_cpu") or 1),
            files=vim.vm.FileInfo(vmPathName=vm_path),
            deviceChange=device_changes,
            annotation=f"Restored from backup on {self.host}",
        )
        config_spec.firmware = requested_firmware

        # Secure Boot only applies to EFI firmware; vCenter rejects/ignores it
        # on a BIOS VM, so a source value of True on a non-EFI restore target
        # (e.g. an explicit firmware override) is dropped rather than sent.
        requested_secure_boot = config_metadata.get("secure_boot_enabled")
        if requested_secure_boot is not None:
            if requested_firmware == "efi":
                config_spec.bootOptions = vim.vm.BootOptions(efiSecureBootEnabled=bool(requested_secure_boot))
            elif requested_secure_boot:
                logger.warning(
                    "Source VM '%s' had Secure Boot enabled but restore firmware is '%s'; "
                    "Secure Boot requires EFI and will not be enabled on the restored VM.",
                    vm_name,
                    requested_firmware,
                )
        logger.info(
            "Creating VM shell '%s' with SCSI controller type '%s' and Secure Boot requested=%s.",
            vm_name,
            requested_scsi_controller_type,
            requested_secure_boot,
        )

        task = datacenter.vmFolder.CreateVM_Task(config=config_spec, pool=resource_pool, host=host)
        vm = self._wait_for_task(task)
        resulting_firmware = _normalize_vm_firmware(
            getattr(getattr(vm, "config", None), "firmware", None)
        )
        if resulting_firmware is None and getattr(vm, "_moId", None):
            refreshed_vm = self._get_vm_by_moref(str(getattr(vm, "_moId", "") or ""))
            resulting_firmware = _normalize_vm_firmware(
                getattr(getattr(refreshed_vm, "config", None), "firmware", None)
            )
        logger.info(
            "Created VM shell '%s' on host '%s' with requested firmware '%s'; resulting firmware '%s'.",
            vm_name,
            host_name,
            requested_firmware,
            resulting_firmware or "unknown",
        )
        if resulting_firmware is not None and resulting_firmware != requested_firmware:
            raise RuntimeError(
                "Restored VM firmware mismatch for "
                f"'{vm_name}': requested {requested_firmware}, got {resulting_firmware}"
            )
        return {
            "vm_name": vm_name,
            "vm_moref": vm._moId,
            "host_name": host_name,
            "datastore_name": datastore_name,
            "disk_paths": created_disk_paths,
            "requested_firmware": requested_firmware,
            "restored_firmware": resulting_firmware,
            "requested_scsi_controller_type": requested_scsi_controller_type,
            "requested_secure_boot": bool(requested_secure_boot) if requested_secure_boot is not None else None,
        }

    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        snapshot_list = vm.snapshot.rootSnapshotList if vm.snapshot else []
        target = self._find_snapshot(snapshot_list, snapshot_name)

        if not target:
            logger.warning(
                "Snapshot '%s' not found on VM '%s' - already removed or never created.",
                snapshot_name,
                vm_name,
            )
            return

        task = target.snapshot.RemoveSnapshot_Task(removeChildren=False)
        self._wait_for_task(task)
        logger.info("Snapshot '%s' removed from VM '%s'.", snapshot_name, vm_name)

    def attach_existing_disk(
        self,
        vm_name: str,
        datastore_path: str,
        disk_label: str | None = None,
        disk_uuid: str | None = None,
    ) -> dict[str, Any]:
        vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        controller = None
        used_unit_numbers: set[int] = set()
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualSCSIController):
                controller = device
            if isinstance(device, vim.vm.device.VirtualDisk):
                unit_number = getattr(device, "unitNumber", None)
                if isinstance(unit_number, int):
                    used_unit_numbers.add(unit_number)

        if controller is None:
            raise ValueError(f"VM '{vm_name}' does not have a SCSI controller.")

        unit_number = None
        for candidate in range(16):
            if candidate == 7:
                continue
            if candidate not in used_unit_numbers:
                unit_number = candidate
                break
        if unit_number is None:
            raise ValueError(f"VM '{vm_name}' has no free SCSI unit slots.")

        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add

        disk = vim.vm.device.VirtualDisk()
        disk.key = -1
        disk.controllerKey = controller.key
        disk.unitNumber = unit_number
        disk.capacityInKB = 0
        disk.deviceInfo = vim.Description()
        disk.deviceInfo.label = disk_label or Path(datastore_path).stem
        disk.deviceInfo.summary = datastore_path

        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        backing.fileName = datastore_path
        backing.diskMode = "persistent"
        backing.thinProvisioned = True
        source_disk_uuid = _normalize_disk_uuid(disk_uuid)
        if source_disk_uuid:
            backing.uuid = source_disk_uuid
        disk.backing = backing
        disk_spec.device = disk

        spec = vim.vm.ConfigSpec(deviceChange=[disk_spec])
        task = vm.ReconfigVM_Task(spec=spec)
        self._wait_for_task(task)
        logger.info("Attached disk '%s' to VM '%s'.", datastore_path, vm_name)
        return {
            "datastore_path": datastore_path,
            "controller_key": controller.key,
            "unit_number": unit_number,
            "label": disk.deviceInfo.label,
        }

    def create_managed_disk(
        self,
        *,
        datastore_name: str,
        disk_path: str,
        capacity_gb: float,
        disk_type_code: int = VddkDiskType.VMFS_THIN,
    ) -> dict[str, Any]:
        if self.si is None:
            raise ValueError("Not connected")

        datastore_name_from_path, folder, filename = _parse_datastore_path_for_api(disk_path)
        if datastore_name_from_path != datastore_name:
            raise ValueError(
                f"Disk path datastore '{datastore_name_from_path}' does not match '{datastore_name}'"
            )

        service_type = self.get_service_type()
        datacenter = None
        if service_type == "VirtualCenter":
            datastore = self._get_datastore(datastore_name)
            if datastore is None:
                raise ValueError(
                    f"Datastore '{datastore_name}' not found on {self.host} while creating managed disk"
                )
            datacenter = self._get_datacenter(datastore)
            if datacenter is None:
                raise ValueError(
                    f"Unable to determine datacenter for datastore '{datastore_name}' on {self.host}"
                )

        file_manager = self.si.RetrieveContent().fileManager
        if folder:
            folder_path = f"[{datastore_name}] {folder}"
            try:
                file_manager.MakeDirectory(
                    name=folder_path,
                    datacenter=datacenter,
                    createParentDirectories=True,
                )
            except vim.fault.FileAlreadyExists:
                pass

        spec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec()
        spec.adapterType = "lsiLogic"
        spec.diskType = hostagent_disk_type_from_code(disk_type_code)
        spec.capacityKb = int(max(capacity_gb, 1.0) * 1024 * 1024)

        task = self.si.RetrieveContent().virtualDiskManager.CreateVirtualDisk_Task(
            name=disk_path,
            datacenter=datacenter,
            spec=spec,
        )
        self._wait_for_task(task)
        logger.info("Created managed disk '%s'.", disk_path)
        return {
            "datastore_path": disk_path,
            "capacity_gb": capacity_gb,
            "file_name": filename,
            "folder": folder,
            "disk_type_code": disk_type_code,
            "disk_type": spec.diskType,
        }

    def detach_disk(self, vm_name: str, datastore_path: str) -> bool:
        vm = self._get_vm(vm_name)
        if not vm:
            return False
        for device in vm.config.hardware.device:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            backing = getattr(device, "backing", None)
            if str(getattr(backing, "fileName", "") or "") != datastore_path:
                continue
            device_spec = vim.vm.device.VirtualDeviceSpec()
            device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
            device_spec.device = device
            task = vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=[device_spec]))
            self._wait_for_task(task)
            logger.info("Detached disk '%s' from VM '%s'.", datastore_path, vm_name)
            return True
        return False

    def delete_managed_disk(self, disk_path: str) -> None:
        if self.si is None:
            raise ValueError("Not connected")

        datastore_name, _, _ = _parse_datastore_path_for_api(disk_path)
        datacenter = None
        if self.get_service_type() == "VirtualCenter":
            datastore = self._get_datastore(datastore_name)
            if datastore is None:
                raise ValueError(
                    f"Datastore '{datastore_name}' not found on {self.host} while deleting managed disk"
                )
            datacenter = self._get_datacenter(datastore)

        task = self.si.RetrieveContent().virtualDiskManager.DeleteVirtualDisk_Task(
            name=disk_path,
            datacenter=datacenter,
        )
        self._wait_for_task(task)
        logger.info("Deleted managed disk '%s'.", disk_path)

    def _vm_matches_orphan_cleanup_fallback(self, vm: Any, error: Exception) -> bool:
        connection_state = str(
            getattr(getattr(vm, "runtime", None), "connectionState", "") or ""
        ).lower()
        if connection_state in {"orphaned", "inaccessible", "invalid"}:
            return True

        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "file",
                "not found",
                "managedobjectnotfound",
                "has already been deleted",
                "cannot access",
                "inaccessible",
                "orphan",
            )
        )

    def destroy_vm(self, vm_name: str, vm_moref: str | None = None) -> None:
        service_type = self.get_service_type()
        vm = self._get_vm_by_moref(vm_moref or "") if vm_moref else None
        if vm is None:
            vm = self._get_vm(vm_name)
        if not vm:
            logger.warning(
                "VM '%s' (moRef=%s) not found on %s - nothing to destroy.",
                vm_name,
                vm_moref or "n/a",
                self.host,
            )
            return

        resolved_name = getattr(vm, "name", vm_name)
        resolved_moref = getattr(vm, "_moId", vm_moref or "n/a")
        power_state = getattr(getattr(vm, "runtime", None), "powerState", None)
        if power_state == vim.VirtualMachinePowerState.poweredOn:
            task = vm.PowerOffVM_Task()
            self._wait_for_task(task)

        try:
            task = vm.Destroy_Task()
            self._wait_for_task(task)
            logger.info(
                "Destroyed VM '%s' (moRef=%s) on %s via %s.",
                resolved_name,
                resolved_moref,
                self.host,
                service_type,
            )
            return
        except Exception as exc:
            if service_type != "VirtualCenter" or not self._vm_matches_orphan_cleanup_fallback(vm, exc):
                raise

            logger.warning(
                "Destroy_Task failed for VM '%s' (moRef=%s) on %s via %s: %s. "
                "Falling back to inventory unregister for orphaned/inaccessible VM cleanup.",
                resolved_name,
                resolved_moref,
                self.host,
                service_type,
                exc,
            )
            try:
                vm.Unregister()
            except Exception as unregister_exc:
                missing_markers = (
                    "not found",
                    "managedobjectnotfound",
                    "has already been deleted",
                )
                if any(marker in str(unregister_exc).lower() for marker in missing_markers):
                    logger.info(
                        "VM '%s' (moRef=%s) was already absent from vCenter inventory during unregister cleanup.",
                        resolved_name,
                        resolved_moref,
                    )
                    return
                raise RuntimeError(
                    "Failed to remove vCenter inventory object for "
                    f"VM '{resolved_name}' (moRef={resolved_moref}) after Destroy_Task failure: {unregister_exc}"
                ) from unregister_exc

            logger.info(
                "Unregistered VM '%s' (moRef=%s) from vCenter inventory after Destroy_Task failure.",
                resolved_name,
                resolved_moref,
            )

    def power_off_vm(self, vm_name: str, vm_moref: str | None = None) -> dict[str, Any]:
        """Power off a VM without destroying it — the standalone half of the
        inline PowerOffVM_Task step destroy_vm already performs, extracted
        for reuse by in-place restore's swap sequence (the original must be
        powered off before it's destroyed and replaced)."""
        vm = self._get_vm_by_moref(vm_moref or "") if vm_moref else None
        if vm is None:
            vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        resolved_name = getattr(vm, "name", vm_name)
        resolved_moref = getattr(vm, "_moId", vm_moref or "")
        power_state = getattr(getattr(vm, "runtime", None), "powerState", None)
        if power_state != vim.VirtualMachinePowerState.poweredOn:
            return {
                "vm_name": resolved_name,
                "vm_moref": resolved_moref,
                "power_state": "poweredOff",
                "changed": False,
            }

        task = vm.PowerOffVM_Task()
        self._wait_for_task(task)
        logger.info(
            "Powered off VM '%s' (moRef=%s) on %s.",
            resolved_name,
            resolved_moref,
            self.host,
        )
        return {
            "vm_name": resolved_name,
            "vm_moref": resolved_moref,
            "power_state": "poweredOff",
            "changed": True,
        }

    def power_on_vm(self, vm_name: str, vm_moref: str | None = None) -> dict[str, Any]:
        vm = self._get_vm_by_moref(vm_moref or "") if vm_moref else None
        if vm is None:
            vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        resolved_name = getattr(vm, "name", vm_name)
        resolved_moref = getattr(vm, "_moId", vm_moref or "")
        power_state = getattr(getattr(vm, "runtime", None), "powerState", None)
        if power_state == vim.VirtualMachinePowerState.poweredOn:
            return {
                "vm_name": resolved_name,
                "vm_moref": resolved_moref,
                "power_state": "poweredOn",
                "changed": False,
            }

        task = vm.PowerOnVM_Task()
        self._wait_for_task(task)
        logger.info(
            "Powered on VM '%s' (moRef=%s) on %s.",
            resolved_name,
            resolved_moref,
            self.host,
        )
        return {
            "vm_name": resolved_name,
            "vm_moref": resolved_moref,
            "power_state": "poweredOn",
            "changed": True,
        }

    def read_vm_identity(self, vm_name: str, vm_moref: str | None = None) -> dict[str, str]:
        vm = self._get_vm_by_moref(vm_moref or "") if vm_moref else None
        if vm is None:
            vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")
        config = getattr(vm, "config", None)
        return {
            "uuid": str(getattr(config, "uuid", "") or ""),
            "instance_uuid": str(getattr(config, "instanceUuid", "") or ""),
        }

    def set_vm_identity(
        self,
        vm_name: str,
        *,
        uuid: str = "",
        instance_uuid: str = "",
        vm_moref: str | None = None,
    ) -> dict[str, Any]:
        if not uuid and not instance_uuid:
            return {"changed": False}
        vm = self._get_vm_by_moref(vm_moref or "") if vm_moref else None
        if vm is None:
            vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        spec = vim.vm.ConfigSpec()
        if uuid:
            spec.uuid = uuid
        if instance_uuid:
            spec.instanceUuid = instance_uuid
        task = vm.ReconfigVM_Task(spec=spec)
        self._wait_for_task(task)
        logger.info(
            "Restored the original identity of VM '%s' on %s: uuid=%s instanceUuid=%s.",
            vm_name,
            self.host,
            uuid or "unchanged",
            instance_uuid or "unchanged",
        )
        return {"changed": True, "uuid": uuid, "instance_uuid": instance_uuid}

    def rename_vm(self, vm_name: str, new_name: str, vm_moref: str | None = None) -> dict[str, Any]:
        """Rename a VM in place via Rename_Task — promotes an in-place
        restore's staging VM to the original's exact name once the original
        has been retired."""
        vm = self._get_vm_by_moref(vm_moref or "") if vm_moref else None
        if vm is None:
            vm = self._get_vm(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found on {self.host}.")

        resolved_moref = getattr(vm, "_moId", vm_moref or "")
        task = vm.Rename_Task(newName=new_name)
        self._wait_for_task(task)
        logger.info(
            "Renamed VM '%s' (moRef=%s) to '%s' on %s.",
            vm_name,
            resolved_moref,
            new_name,
            self.host,
        )
        return {
            "vm_name": new_name,
            "vm_moref": resolved_moref,
            "previous_name": vm_name,
            "changed": True,
        }

    def get_all_vms(self) -> List[Dict[str, Any]]:
        vms = []
        for vm in self._list_vms():
            summary = vm.summary
            firmware = _normalize_vm_firmware(getattr(getattr(vm, "config", None), "firmware", None))
            vms.append(
                {
                    "vm_id": summary.config.uuid,
                    "name": summary.config.name,
                    "power_state": summary.runtime.powerState,
                    "num_cpu": summary.config.numCpu,
                    "memory_mb": summary.config.memorySizeMB,
                    "guest_os": summary.config.guestFullName,
                    "firmware": firmware,
                }
            )
        return vms

    def get_inventory(self) -> Dict[str, List[Dict[str, Any]]]:
        inventory: Dict[str, List[Dict[str, Any]]] = {
            "virtual_machines": [],
            "disks": [],
            "network_adapters": [],
        }
        # Resolving a host MoRef's name is a round-trip, and VMs cluster onto a
        # handful of hosts — cache by MoRef so it costs one call per host.
        host_names: Dict[str, str | None] = {}

        def _running_host_name(runtime: Any) -> str | None:
            """ESXi host the VM runs on. The inventory tree nests VMs under their
            host, so without this every VM on a multi-host vCenter is unplaceable."""
            ref = getattr(runtime, "host", None)
            if ref is None:
                return None
            key = str(getattr(ref, "_moId", None) or ref)
            if key not in host_names:
                try:
                    host_names[key] = str(getattr(ref, "name", "") or "") or None
                except Exception:  # host gone/unreachable — not worth failing discovery
                    host_names[key] = None
            return host_names[key]

        for vm in self._list_vms():
            summary = vm.summary
            config = summary.config
            vm_display_name = getattr(config, "name", None) or getattr(vm, "name", None) or "<unknown>"

            try:
                if getattr(vm, "config", None) is None:
                    raise RuntimeError(
                        "vm.config is unavailable; VM is likely inaccessible or orphaned in vCenter"
                    )

                runtime = summary.runtime
                vm_uuid = config.uuid
                firmware = _normalize_vm_firmware(vm.config.firmware)

                vm_entry = {
                    "vm_id": vm_uuid,
                    "name": config.name,
                    "power_state": str(runtime.powerState),
                    "num_cpu": config.numCpu,
                    "memory_mb": config.memorySizeMB,
                    "guest_os": config.guestFullName,
                    "firmware": firmware,
                    "host": _running_host_name(runtime),
                }

                disk_entries = []
                for disk_index, disk in enumerate(self._collect_disks(vm), start=1):
                    disk_uuid = disk.get("uuid")
                    disk_entries.append(
                        {
                            "vm_id": vm_uuid,
                            "external_id": f"{vm_uuid}-disk-{disk_index}-{disk_uuid or 'N/A'}",
                            "name": disk["disk_label"],
                            "capacity_gb": disk["capacity_gb"],
                            "datastore_path": disk["datastore_path"],
                            "uuid": disk_uuid,
                            "source_disk_type_code": disk.get("source_disk_type_code"),
                            "source_disk_type_name": disk.get("source_disk_type_name"),
                        }
                    )

                nic_entries = []
                for nic_index, device in enumerate(vm.config.hardware.device, start=1):
                    if not isinstance(device, vim.vm.device.VirtualEthernetCard):
                        continue

                    backing = getattr(device, "backing", None)
                    network_name = getattr(backing, "deviceName", None) or getattr(backing, "network", None)
                    nic_entries.append(
                        {
                            "vm_id": vm_uuid,
                            "external_id": f"{vm_uuid}-nic-{nic_index}",
                            "name": device.deviceInfo.label,
                            "mac": getattr(device, "macAddress", None),
                            "network": str(network_name) if network_name is not None else None,
                            "connected": bool(getattr(device.connectable, "connected", False)),
                        }
                    )
            except Exception as exc:
                logger.warning(
                    "Skipping inaccessible/orphaned VM '%s' during vCenter inventory discovery on %s: %s",
                    vm_display_name,
                    self.host,
                    exc,
                )
                continue

            inventory["virtual_machines"].append(vm_entry)
            inventory["disks"].extend(disk_entries)
            inventory["network_adapters"].extend(nic_entries)

        return inventory


def _sanitize_path_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(".-") or "vm"


def _parse_datastore_path_for_api(datastore_path: str) -> tuple[str, str, str]:
    if not datastore_path.startswith("[") or "]" not in datastore_path:
        raise ValueError(f"Unsupported datastore path format: {datastore_path}")
    datastore, relative_path = datastore_path.split("]", 1)
    datastore_name = datastore[1:].strip()
    relative_path = relative_path.strip()
    folder = str(Path(relative_path).parent)
    if folder == ".":
        folder = ""
    return datastore_name, folder, Path(relative_path).name

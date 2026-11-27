"""Stage 0 deterministic snapshot + Stage 1 rule-based classification. No LLM here."""
import re
from datetime import timedelta

from kubernetes import client

from . import config, store

SQLSTATE_DATA = re.compile(r"\b(23\d{3}|42\d{3}|40P01|55P03|08\d{3})\b")


def snapshot(service_name: str, window_start=None, window_end=None) -> dict:
    api = client.CoreV1Api()
    observed = store.now()
    window_start = store.event_time(window_start) or observed - timedelta(seconds=config.INCIDENT_WINDOW_SECONDS)
    window_end = store.event_time(window_end) or observed
    errors = []
    try:
        all_events = api.list_namespaced_event(config.NAMESPACE).items
    except Exception as error:
        all_events = []
        errors.append({"source": "events", "error": type(error).__name__})
    services = {}
    nodes = {}
    for svc in config.services():
        name = svc["name"]
        entry = {"service": name, "prod_commit": svc.get("prod_commit"), "pods": [],
                 "ready_endpoints": None, "events": [], "errors": []}
        services[name] = entry
        try:
            selected = api.list_namespaced_pod(config.NAMESPACE, label_selector=svc["pod_selector"]).items
            for pod in selected:
                statuses = pod.status.container_statuses or []
                containers = []
                for status in statuses:
                    last = status.last_state.terminated if status.last_state else None
                    waiting = status.state.waiting if status.state else None
                    current = status.state.terminated if status.state else None
                    finished = store.event_time(last.finished_at) if last else None
                    containers.append({"name": status.name, "ready": status.ready, "restarts": status.restart_count,
                                       "waiting_reason": waiting.reason if waiting else None,
                                       "termination_reason": current.reason if current else None,
                                       "last_termination_reason": last.reason if last else None,
                                       "last_termination_at": finished,
                                       "last_termination_in_window": bool(finished and window_start <= finished <= window_end)})
                resources = [{"name": container.name, "limits": container.resources.limits if container.resources else None,
                              "requests": container.resources.requests if container.resources else None}
                             for container in pod.spec.containers]
                entry["pods"].append({"pod": pod.metadata.name, "node": pod.spec.node_name, "phase": pod.status.phase,
                                      "ready": all(status.ready for status in statuses) if statuses else None,
                                      "restarts": sum(status.restart_count for status in statuses) if statuses else None,
                                      "containers": containers, "resources": resources,
                                      "git_commit_annotation": (pod.metadata.annotations or {}).get("git-commit")})
                if not statuses:
                    entry["errors"].append({"source": "container_status", "pod": pod.metadata.name, "error": "unavailable"})
                if not resources or any(not resource["limits"] for resource in resources):
                    entry["errors"].append({"source": "resource_limits", "pod": pod.metadata.name, "error": "unavailable"})
                node_name = pod.spec.node_name
                if not node_name:
                    entry["errors"].append({"source": "node", "pod": pod.metadata.name, "error": "unscheduled"})
                elif node_name not in nodes:
                    try:
                        node = api.read_node(node_name)
                        nodes[node_name] = {"conditions": [{"type": condition.type, "status": condition.status}
                                                          for condition in (node.status.conditions or [])],
                                            "capacity": node.status.capacity, "allocatable": node.status.allocatable}
                        missing = [field for field in ("conditions", "capacity", "allocatable") if not nodes[node_name][field]]
                        if missing:
                            nodes[node_name]["missing"] = missing
                    except Exception as error:
                        nodes[node_name] = {"error": type(error).__name__}
        except Exception as error:
            entry["errors"].append({"source": "pods", "error": type(error).__name__})
        pod_names = {pod["pod"] for pod in entry["pods"]}
        for event in all_events:
            times = [store.event_time(value) for value in (event.event_time, event.last_timestamp, event.first_timestamp,
                     event.series.last_observed_time if event.series else None, event.metadata.creation_timestamp)]
            event_at = max((value for value in times if value is not None), default=None)
            if event.involved_object and (event.involved_object.name in pod_names or event.involved_object.name == name
                                          or event.involved_object.name.startswith(name + "-")):
                if event_at is None:
                    entry["errors"].append({"source": "event_timestamp", "object": event.involved_object.name, "error": "unavailable"})
                elif window_start <= event_at <= window_end:
                    entry["events"].append({"reason": event.reason, "message": event.message, "type": event.type,
                                            "object": event.involved_object.name, "event_at": event_at})
        entry["events"] = sorted(entry["events"], key=lambda event: event["event_at"])[-15:]
        try:
            endpoints = api.read_namespaced_endpoints(name, config.NAMESPACE)
            entry["ready_endpoints"] = sum(len(subset.addresses or []) for subset in (endpoints.subsets or []))
        except Exception as error:
            entry["errors"].append({"source": "endpoints", "error": type(error).__name__})
        try:
            deployment = client.AppsV1Api().read_namespaced_deployment(name, config.NAMESPACE)
            entry["deployment"] = {"generation": deployment.metadata.generation,
                                   "observed_generation": deployment.status.observed_generation,
                                   "replicas": deployment.status.replicas, "ready_replicas": deployment.status.ready_replicas,
                                   "annotations": deployment.spec.template.metadata.annotations}
        except Exception as error:
            entry["deployment"] = {"error": type(error).__name__}
    resources = {}
    metrics_api = client.CustomObjectsApi()
    for resource, scope in (("pods", "namespace"), ("nodes", "cluster")):
        try:
            if scope == "namespace":
                resources[resource] = metrics_api.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", config.NAMESPACE, resource)
            else:
                resources[resource] = metrics_api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", resource)
        except Exception as error:
            resources[resource] = {"error": type(error).__name__, "note": "usage unavailable; limits are not usage"}
    pod_usage = {item.get("metadata", {}).get("name") for item in resources["pods"].get("items", [])}
    node_usage = {item.get("metadata", {}).get("name") for item in resources["nodes"].get("items", [])}
    for entry in services.values():
        for pod in entry["pods"]:
            if pod["pod"] not in pod_usage:
                entry["errors"].append({"source": "pod_usage", "pod": pod["pod"], "error": "unavailable"})
    for node_name, node in nodes.items():
        if node_name not in node_usage:
            node["usage_error"] = "unavailable"
    try:
        with store.db(config.RO_DB_DSN) as conn:
            conn.execute("set statement_timeout = 3000")
            db_health = dict(conn.execute(
                "select count(*) as connections, count(*) filter (where wait_event is not null) as waiting, "
                "count(*) filter (where state='active' and now()-xact_start > interval '30 seconds') as long_tx "
                "from pg_stat_activity").fetchone())
    except Exception as error:
        db_health = {"error": type(error).__name__}
    primary = services.get(service_name, {"service": service_name, "pods": [], "events": [], "ready_endpoints": None})
    return {**primary, "observed_at": observed, "window_start": window_start, "window_end": window_end,
            "services": services, "nodes": nodes, "resource_usage": resources, "db_health": db_health, "collection_errors": errors}


def classify(snap: dict, message: str) -> str:
    containers = [container for pod in snap.get("pods", []) for container in pod.get("containers", [])]
    if any(container.get("waiting_reason") in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}
           or container.get("termination_reason") == "OOMKilled"
           or (container.get("last_termination_in_window") and container.get("last_termination_reason") == "OOMKilled")
           for container in containers) or any(event.get("reason") in {"BackOff", "FailedScheduling", "ErrImagePull", "Evicted"}
                                              for event in snap.get("events", [])):
        return "infra"
    lower = message.lower()
    if snap.get("ready_endpoints") == 0 or "connection refused" in lower or re.search(r"\b503\b", message) \
            or "service unavailable" in lower or "ResourceAccessException" in message:
        return "dependency"
    if SQLSTATE_DATA.search(message) or "SQL" in message or "column" in message.lower() or "duplicate key" in message.lower():
        return "data"
    if "Exception" in message:
        return "code"
    return "unknown"

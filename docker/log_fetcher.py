from fastapi import FastAPI, HTTPException
import paramiko
import os
import json
import re
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Kubernetes AI Observability API")

# ==============================
# ENV CONFIG
# ==============================

SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = int(os.getenv("SSH_PORT", 22))
SSH_USER = os.getenv("SSH_USER")
SSH_PASSWORD = os.getenv("SSH_PASSWORD")

K8S_MASTER = os.getenv("K8S_MASTER")
K8S_USER = os.getenv("K8S_USER")

# ==============================
# SSH EXECUTION
# ==============================

def execute_remote_command(command: str) -> str:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASSWORD,
            timeout=10
        )

        full_cmd = f"ssh {K8S_USER}@{K8S_MASTER} \"{command}\""
        stdin, stdout, stderr = ssh.exec_command(full_cmd)

        output = stdout.read().decode()
        error = stderr.read().decode()

        if error and not output:
            raise Exception(error.strip())

        return output.strip()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        ssh.close()

# ==============================
# HEALTH
# ==============================

@app.get("/")
def health():
    return {"status": "Kubernetes AI Observability API running"}

# ==============================
# NAMESPACES
# ==============================

@app.get("/namespaces")
def get_namespaces():

    raw = execute_remote_command("kubectl get ns -o json")
    data = json.loads(raw)

    return [
        {
            "name": item["metadata"]["name"],
            "status": item["status"]["phase"]
        }
        for item in data.get("items", [])
    ]

# ==============================
# CLUSTER OVERVIEW
# ==============================

@app.get("/cluster/overview")
def cluster_overview():

    raw = execute_remote_command("kubectl get pods -A -o json")
    data = json.loads(raw)

    pods = data.get("items", [])

    summary = {
        "total": len(pods),
        "running": 0,
        "failed": 0,
        "crashloop": 0,
        "pending": 0
    }

    issues = []

    for p in pods:
        status = p["status"]["phase"]

        if status == "Running":
            summary["running"] += 1
        elif status == "Pending":
            summary["pending"] += 1
        else:
            summary["failed"] += 1

        for c in p["status"].get("containerStatuses", []):
            waiting = c.get("state", {}).get("waiting", {})
            if waiting.get("reason") == "CrashLoopBackOff":
                summary["crashloop"] += 1
                issues.append({
                    "namespace": p["metadata"]["namespace"],
                    "pod": p["metadata"]["name"],
                    "reason": "CrashLoopBackOff"
                })

    return {
        "summary": summary,
        "critical_issues": issues[:20]
    }

# ==============================
# PODS (NAMESPACE)
# ==============================

@app.get("/pods")
def get_pods(namespace: str):

    raw = execute_remote_command(
        f"kubectl get pods -n {namespace} -o json"
    )

    data = json.loads(raw)

    pods = []

    for item in data.get("items", []):
        status = item["status"]["phase"]

        restarts = sum(
            c.get("restartCount", 0)
            for c in item["status"].get("containerStatuses", [])
        )

        pods.append({
            "name": item["metadata"]["name"],
            "namespace": namespace,
            "status": status,
            "node": item["spec"].get("nodeName"),
            "restarts": restarts,
            "is_healthy": status == "Running" and restarts == 0
        })

    summary = {
        "total": len(pods),
        "running": len([p for p in pods if p["status"] == "Running"]),
        "failed": len([p for p in pods if p["status"] != "Running"])
    }

    return {
        "summary": summary,
        "pods": pods
    }

# ==============================
# LOG ANALYSIS
# ==============================

@app.get("/logs")
def get_logs(namespace: str, pod: str, since: str = "10m"):

    current = execute_remote_command(
        f"kubectl logs -n {namespace} {pod} --since={since} --tail=200"
    )

    previous = execute_remote_command(
        f"kubectl logs -n {namespace} {pod} --previous --tail=50"
    )

    parsed, errors, warns = [], [], []

    for line in current.splitlines():
        entry = {"message": line}

        if re.search(r"error|fail|exception", line, re.I):
            entry["level"] = "ERROR"
            errors.append(entry)
        elif re.search(r"warn", line, re.I):
            entry["level"] = "WARN"
            warns.append(entry)
        else:
            entry["level"] = "INFO"

        parsed.append(entry)

    return {
        "pod": pod,
        "time_window": since,
        "error_count": len(errors),
        "warning_count": len(warns),
        "recent_errors": errors[:10],
        "recent_warnings": warns[:10],
        "last_logs": parsed[-50:],
        "previous_crash_logs": previous.splitlines()[:30]
    }

# ==============================
# LOG STREAM (PSEUDO REAL-TIME)
# ==============================

@app.get("/logs/stream")
def stream_logs(namespace: str, pod: str):

    raw = execute_remote_command(
        f"kubectl logs -n {namespace} {pod} --tail=100"
    )

    return {
        "pod": pod,
        "stream": raw.splitlines()
    }

# ==============================
# EVENTS (NAMESPACE)
# ==============================

@app.get("/events")
def get_events(namespace: str):

    raw = execute_remote_command(
        f"kubectl get events -n {namespace} -o json"
    )

    data = json.loads(raw)

    events = []

    for item in data.get("items", []):
        events.append({
            "type": item.get("type"),
            "reason": item.get("reason"),
            "message": item.get("message"),
            "object": item.get("involvedObject", {}).get("name"),
            "time": item.get("lastTimestamp") or item.get("eventTime")
        })

    events = sorted(events, key=lambda x: x["time"] or "", reverse=True)

    return {
        "total_events": len(events),
        "critical": [e for e in events if e["type"] == "Warning"][:20],
        "events": events[:50]
    }

# ==============================
# CLUSTER EVENTS
# ==============================

@app.get("/cluster/events")
def cluster_events():

    raw = execute_remote_command(
        "kubectl get events -A -o json"
    )

    data = json.loads(raw)

    events = []

    for item in data.get("items", []):
        events.append({
            "namespace": item["metadata"]["namespace"],
            "type": item.get("type"),
            "reason": item.get("reason"),
            "message": item.get("message"),
            "object": item.get("involvedObject", {}).get("name"),
            "time": item.get("lastTimestamp") or item.get("eventTime")
        })

    events = sorted(events, key=lambda x: x["time"] or "", reverse=True)

    return {
        "total": len(events),
        "warnings": [e for e in events if e["type"] == "Warning"][:50],
        "events": events[:50]
    }

# ==============================
# POD YAML
# ==============================

@app.get("/pod/yaml")
def get_pod_yaml(namespace: str, pod: str):

    raw = execute_remote_command(
        f"kubectl get pod {pod} -n {namespace} -o yaml"
    )

    return {
        "pod": pod,
        "yaml": raw
    }

# ==============================
# DESCRIBE POD
# ==============================

@app.get("/describe")
def describe_pod(namespace: str, pod: str):

    raw = execute_remote_command(
        f"kubectl describe pod {pod} -n {namespace}"
    )

    lines = raw.splitlines()

    issues = []
    events = []

    capture_events = False

    for line in lines:
        if "Events:" in line:
            capture_events = True
            continue

        if capture_events and line.strip():
            events.append(line.strip())

        if any(x in line for x in ["Warning", "Failed", "Error", "BackOff"]):
            issues.append(line.strip())

    return {
        "pod": pod,
        "issues_detected": issues[:20],
        "recent_events": events[:20]
    }

# ==============================
# NAMESPACE DIAGNOSTICS (AI CORE)
# ==============================

@app.get("/namespace/diagnostics")
def namespace_diagnostics(namespace: str):

    pods_data = get_pods(namespace)
    events_data = get_events(namespace)

    diagnostics = []

    for pod in pods_data["pods"]:
        logs = get_logs(namespace, pod["name"])

        issues = []

        if pod["restarts"] > 0:
            issues.append(f"Restarts: {pod['restarts']}")

        if logs["error_count"] > 0:
            issues.append(f"{logs['error_count']} errors detected")

        if not pod["is_healthy"]:
            issues.append("Pod not healthy")

        diagnostics.append({
            "pod": pod["name"],
            "status": pod["status"],
            "node": pod["node"],
            "issues": issues,
            "insight": generate_insight(pod, logs)
        })

    return {
        "namespace": namespace,
        "summary": pods_data["summary"],
        "pod_analysis": diagnostics,
        "critical_events": events_data["critical"],
        "health": evaluate_namespace_health(diagnostics)
    }

# ==============================
# AI REASONING LAYER
# ==============================

def generate_insight(pod, logs):

    insights = []

    if pod["restarts"] > 0:
        insights.append("Pod restarted → instability")

    if logs["error_count"] > 5:
        insights.append("High error rate")

    if logs["previous_crash_logs"]:
        insights.append("Previous crash detected")

    if pod["status"] != "Running":
        insights.append("Not running")

    if not insights:
        return "Healthy"

    return " | ".join(insights)


def evaluate_namespace_health(diags):

    issues = [d for d in diags if d["issues"]]

    if not issues:
        return "Healthy"

    if len(issues) < 3:
        return "Degraded"

    return "Critical"

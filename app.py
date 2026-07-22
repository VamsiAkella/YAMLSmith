from flask import Flask, render_template, request, jsonify, send_file
import yaml
import jinja2
import io
import zipfile
from typing import Dict, Any, List, Tuple

app = Flask(__name__)

# Custom YAML Dumper to preserve multi-line string formatting and clean output
class CleanYamlDumper(yaml.SafeDumper):
    def represent_str(self, data):
        if '\n' in data:
            return self.represent_scalar('tag:yaml.org,2002:str', data, style='|')
        return self.represent_scalar('tag:yaml.org,2002:str', data)

def dump_yaml(data: Dict[str, Any]) -> str:
    """Helper to convert dictionary to clean YAML string."""
    return yaml.dump(data, Dumper=CleanYamlDumper, default_flow_style=False, sort_keys=False)

def build_manifests(data: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Core engine to construct Kubernetes resources based on form inputs.
    Returns:
        - List of dicts: [{"filename": "00-namespace.yaml", "content": "..."}]
        - List of warning string messages
    """
    manifests = []
    warnings = []

    # Extract cleaned form parameters
    app_name = data.get("appName", "my-app").strip().lower()
    namespace = data.get("namespace", "default").strip().lower()
    resource_type = data.get("resourceType", "Deployment")
    image = data.get("image", "").strip()
    replicas = int(data.get("replicas", 1))
    
    expose_service = bool(data.get("exposeService", False))
    service_type = data.get("serviceType", "ClusterIP")
    service_port = int(data.get("servicePort", 80))
    target_port = int(data.get("targetPort", 8080))
    
    enable_ingress = bool(data.get("enableIngress", False))
    ingress_host = data.get("ingressHost", "").strip()
    
    enable_autoscaling = bool(data.get("enableAutoscaling", False))
    min_replicas = int(data.get("minReplicas", 2))
    max_replicas = int(data.get("maxReplicas", 10))
    cpu_target = int(data.get("cpuTarget", 80))
    
    enable_storage = bool(data.get("enableStorage", False))
    storage_size = data.get("storageSize", "10Gi").strip()
    storage_class = data.get("storageClass", "standard").strip()
    mount_path = data.get("mountPath", "/data").strip()
    
    enable_configmap = bool(data.get("enableConfigMap", False))
    config_map_data = data.get("configMapData", {})
    
    enable_secret = bool(data.get("enableSecret", False))
    secret_data = data.get("secretData", {})
    
    enable_probes = bool(data.get("enableProbes", False))
    liveness_path = data.get("livenessPath", "/health").strip()
    readiness_path = data.get("readinessPath", "/ready").strip()
    
    cpu_request = data.get("cpuRequest", "").strip()
    memory_request = data.get("memoryRequest", "").strip()
    cpu_limit = data.get("cpuLimit", "").strip()
    memory_limit = data.get("memoryLimit", "").strip()

    # Image-based heuristics / warnings
    if "postgres" in image.lower() and not enable_storage:
        enable_storage = True
        warnings.append("Auto-enabled Persistent Storage for PostgreSQL image to prevent data loss.")

    if "redis" in image.lower() and not enable_probes:
        enable_probes = True
        warnings.append("Auto-enabled Health Probes for Redis workload.")

    if namespace == "production" and replicas < 2 and resource_type in ["Deployment", "StatefulSet"]:
        warnings.append("Production workloads should run at least 2 replicas for High Availability.")

    if not cpu_limit or not memory_limit:
        warnings.append("Missing resource limits can cause noisy-neighbor issues on K8s nodes.")

    # Shared Standard Labels
    labels = {
        "app": app_name,
        "app.kubernetes.io/name": app_name,
        "app.kubernetes.io/instance": f"{app_name}-{namespace}"
    }

    # 1. Namespace Manifest
    if namespace != "default":
        ns_dict = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": labels
            }
        }
        manifests.append({
            "filename": "00-namespace.yaml",
            "content": f"# Created to isolate {app_name} within the {namespace} environment\n" + dump_yaml(ns_dict)
        })

    # 2. ConfigMap Manifest
    if enable_configmap and config_map_data:
        cm_dict = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{app_name}-config",
                "namespace": namespace,
                "labels": labels
            },
            "data": config_map_data
        }
        manifests.append({
            "filename": "01-configmap.yaml",
            "content": "# Non-sensitive configuration key-value pairs\n" + dump_yaml(cm_dict)
        })

    # 3. Secret Manifest
    if enable_secret and secret_data:
        sec_dict = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{app_name}-secret",
                "namespace": namespace,
                "labels": labels
            },
            "type": "Opaque",
            "stringData": secret_data
        }
        manifests.append({
            "filename": "02-secret.yaml",
            "content": "# Sensitive string parameters automatically populated via K8s stringData\n" + dump_yaml(sec_dict)
        })

    # 4. Storage (PVC)
    pvc_name = f"{app_name}-pvc"
    if enable_storage and resource_type != "StatefulSet":
        pvc_dict = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc_name,
                "namespace": namespace,
                "labels": labels
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": storage_size
                    }
                }
            }
        }
        if storage_class:
            pvc_dict["spec"]["storageClassName"] = storage_class

        manifests.append({
            "filename": "03-pvc.yaml",
            "content": "# Durable block or network storage claim\n" + dump_yaml(pvc_dict)
        })

    # 5. Core Workload Object (Deployment/StatefulSet/DaemonSet/Job/CronJob)
    container_spec = {
        "name": app_name,
        "image": image,
    }

    # Ports
    if expose_service:
        container_spec["ports"] = [{"containerPort": target_port}]

    # Environment Binding from ConfigMap/Secrets
    env_from = []
    if enable_configmap and config_map_data:
        env_from.append({"configMapRef": {"name": f"{app_name}-config"}})
    if enable_secret and secret_data:
        env_from.append({"secretRef": {"name": f"{app_name}-secret"}})
    if env_from:
        container_spec["envFrom"] = env_from

    # Resources
    resources = {}
    if cpu_request or memory_request:
        resources["requests"] = {}
        if cpu_request: resources["requests"]["cpu"] = cpu_request
        if memory_request: resources["requests"]["memory"] = memory_request
    if cpu_limit or memory_limit:
        resources["limits"] = {}
        if cpu_limit: resources["limits"]["cpu"] = cpu_limit
        if memory_limit: resources["limits"]["memory"] = memory_limit
    if resources:
        container_spec["resources"] = resources

    # Probes
    if enable_probes:
        if liveness_path:
            container_spec["livenessProbe"] = {
                "httpGet": {"path": liveness_path, "port": target_port},
                "initialDelaySeconds": 15,
                "periodSeconds": 20
            }
        if readiness_path:
            container_spec["readinessProbe"] = {
                "httpGet": {"path": readiness_path, "port": target_port},
                "initialDelaySeconds": 5,
                "periodSeconds": 10
            }

    # Volumes & Mounts
    volumes = []
    if enable_storage:
        container_spec["volumeMounts"] = [{
            "name": "data-volume",
            "mountPath": mount_path
        }]
        if resource_type != "StatefulSet":
            volumes.append({
                "name": "data-volume",
                "persistentVolumeClaim": {"claimName": pvc_name}
            })

    # 1. Build the dynamic security context based on user selection
    security_context = {}

    if data.get("runAsNonRoot"):
        security_context["runAsNonRoot"] = True
        security_context["runAsUser"] = 10001  # Forces K8s to run as non-root UID 10001

    # 2. Construct the pod spec
    pod_spec = {
        "containers": [container_spec]
    }

    # 3. Only attach securityContext if there's actually something in it
    if security_context:
        pod_spec["securityContext"] = security_context

    if volumes:
        pod_spec["volumes"] = volumes

    # Build top-level primary workload manifest
    api_version = "apps/v1"
    if resource_type in ["Job", "CronJob"]:
        api_version = "batch/v1"

    workload_dict = {
        "apiVersion": api_version,
        "kind": resource_type,
        "metadata": {
            "name": app_name,
            "namespace": namespace,
            "labels": labels
        }
    }

    if resource_type in ["Deployment", "StatefulSet", "DaemonSet"]:
        workload_dict["spec"] = {
            "selector": {"matchLabels": {"app": app_name}},
            "template": {
                "metadata": {"labels": {"app": app_name}},
                "spec": pod_spec
            }
        }
        if resource_type != "DaemonSet":
            workload_dict["spec"]["replicas"] = replicas

        # StatefulSet VolumeClaimTemplates
        if resource_type == "StatefulSet" and enable_storage:
            vct = {
                "metadata": {"name": "data-volume"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": storage_size}}
                }
            }
            if storage_class:
                vct["spec"]["storageClassName"] = storage_class
            workload_dict["spec"]["volumeClaimTemplates"] = [vct]

    elif resource_type == "Job":
        workload_dict["spec"] = {
            "template": {
                "metadata": {"labels": {"app": app_name}},
                "spec": {**pod_spec, "restartPolicy": "OnFailure"}
            }
        }

    elif resource_type == "CronJob":
        workload_dict["spec"] = {
            "schedule": "0 * * * *",
            "jobTemplate": {
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": app_name}},
                        "spec": {**pod_spec, "restartPolicy": "OnFailure"}
                    }
                }
            }
        }

    manifests.append({
        "filename": f"04-{resource_type.lower()}.yaml",
        "content": f"# Core {resource_type} Workload Definition\n" + dump_yaml(workload_dict)
    })

    # 6. Service Manifest
    if expose_service:
        svc_dict = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"{app_name}-service",
                "namespace": namespace,
                "labels": labels
            },
            "spec": {
                "type": service_type,
                "selector": {"app": app_name},
                "ports": [{
                    "protocol": "TCP",
                    "port": service_port,
                    "targetPort": target_port
                }]
            }
        }
        manifests.append({
            "filename": "05-service.yaml",
            "content": f"# Exposes {app_name} inside/outside the cluster\n" + dump_yaml(svc_dict)
        })

    # 7. Ingress Manifest
    if enable_ingress and expose_service:
        ing_dict = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": f"{app_name}-ingress",
                "namespace": namespace,
                "labels": labels
            },
            "spec": {
                "rules": [{
                    "host": ingress_host if ingress_host else f"{app_name}.local",
                    "http": {
                        "paths": [{
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {
                                "service": {
                                    "name": f"{app_name}-service",
                                    "port": {"number": service_port}
                                }
                            }
                        }]
                    }
                }]
            }
        }
        manifests.append({
            "filename": "06-ingress.yaml",
            "content": "# Layer 7 Routing for HTTP/HTTPS Traffic\n" + dump_yaml(ing_dict)
        })

    # 8. HPA Manifest
    if enable_autoscaling and resource_type in ["Deployment", "StatefulSet"]:
        hpa_dict = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": f"{app_name}-hpa",
                "namespace": namespace,
                "labels": labels
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": resource_type,
                    "name": app_name
                },
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "metrics": [{
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": cpu_target
                        }
                    }
                }]
            }
        }
        manifests.append({
            "filename": "07-hpa.yaml",
            "content": "# Dynamic horizontal pod autoscaler based on CPU consumption\n" + dump_yaml(hpa_dict)
        })

    return manifests, warnings


def validate_inputs(data: Dict[str, Any]) -> List[str]:
    """Strict validation rules returning error strings."""
    errors = []
    
    if not data.get("appName"):
        errors.append("Application Name is required.")
    if not data.get("image"):
        errors.append("Container Image is required.")
        
    res_type = data.get("resourceType")
    if res_type == "StatefulSet" and not data.get("enableStorage"):
        errors.append("StatefulSet requires Persistent Storage to be enabled.")

    if data.get("enableIngress") and not data.get("exposeService"):
        errors.append("Enable Ingress requires 'Expose as Service' to be enabled.")

    if data.get("enableAutoscaling"):
        if not data.get("cpuLimit") or not data.get("memoryLimit"):
            errors.append("Autoscaling (HPA) requires CPU and Memory limits to be set.")

    return errors


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json() or {}
    
    # 1. Check validation errors
    errors = validate_inputs(data)
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    # 2. Build Kubernetes Manifests
    manifests, warnings = build_manifests(data)

    # Combine into single multi-doc YAML for raw previewing
    combined_yaml = "\n---\n".join([m["content"] for m in manifests])

    return jsonify({
        "success": True,
        "warnings": warnings,
        "combinedYaml": combined_yaml,
        "manifests": manifests
    })


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json() or {}
    errors = validate_inputs(data)
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    manifests, _ = build_manifests(data)
    app_name = data.get("appName", "k8s-manifests").strip().lower()

    # Create ZIP file buffer in-memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for manifest in manifests:
            zip_file.writestr(f"{app_name}/{manifest['filename']}", manifest["content"])

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{app_name}-manifests.zip"
    )


if __name__ == "__main__":
    # Local dev runner
    app.run(host="0.0.0.0", port=5000, debug=True)
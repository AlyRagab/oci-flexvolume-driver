#!/usr/bin/env python

import argparse
import datetime
import json
import os
import re
import select
import subprocess
import sys
import time

TMP_OCI_API_KEY = "/tmp/oci_api_key.pem"
TMP_INSTANCE_KEY = "/tmp/instance_key"
DEBUG_FILE = "runner.log"
DRIVER_DIR = "/usr/libexec/kubernetes/kubelet-plugins/volume/exec/oracle~oci"
TERRAFORM_DIR = "terraform"
TIMEOUT = 120

def _check_env():
    if "OCI_API_KEY" not in os.environ and "OCI_API_KEY_VAR" not in os.environ:
        _log("Error. Can't find either OCI_API_KEY or OCI_API_KEY_VAR in the environment.")
        sys.exit(1)
    if "INSTANCE_KEY" not in os.environ and "INSTANCE_KEY_VAR" not in os.environ:
        _log("Error. Can't find either INSTANCE_KEY or INSTANCE_KEY_VAR in the environment.")
        sys.exit(1)
    if "MASTER_IP" not in os.environ:
        _log("Error. Can't find MASTER_IP in the environment.")
        sys.exit(1)
    if "SLAVE0_IP" not in os.environ:
        _log("Error. Can't find SLAVE0_IP in the environment.")
        sys.exit(1)
    if "SLAVE1_IP" not in os.environ:
        _log("Error. Can't find SLAVE1_IP in the environment.")
        sys.exit(1)


def _create_key_files():
    if "OCI_API_KEY_VAR" in os.environ:
        _run_command("echo \"$OCI_API_KEY_VAR\" > " + TMP_OCI_API_KEY, ".")
        _run_command("chmod 600 " + TMP_OCI_API_KEY, ".")
    if "INSTANCE_KEY_VAR" in os.environ:
        _run_command("echo \"$INSTANCE_KEY_VAR\" > " + TMP_INSTANCE_KEY, ".")
        _run_command("chmod 600 " + TMP_INSTANCE_KEY, ".")


def _destroy_key_files():
    if "OCI_API_KEY_VAR" in os.environ:
        os.remove(TMP_OCI_API_KEY)
    if "INSTANCE_KEY_VAR" in os.environ:
        os.remove(TMP_INSTANCE_KEY)


def _get_oci_api_key_file():
    return os.environ['OCI_API_KEY'] if "OCI_API_KEY" in os.environ else TMP_OCI_API_KEY


def _get_instance_key_file():
    return os.environ['INSTANCE_KEY'] if "INSTANCE_KEY" in os.environ else TMP_INSTANCE_KEY


def _banner(as_banner, bold):
    if as_banner:
        if bold:
            print "********************************************************"
        else:
            print "--------------------------------------------------------"


def _reset_debug_file():
    if os.path.exists(DEBUG_FILE):
        os.remove(DEBUG_FILE)


def _debug_file(string):
    with open(DEBUG_FILE, "a") as debug_file:
        debug_file.write(string)


def _log(string, as_banner=False, bold=False):
    _banner(as_banner, bold)
    print string
    _banner(as_banner, bold)


def _process_stream(stream, read_fds, global_buf, line_buf):
    char = stream.read(1)
    if char == '':
        read_fds.remove(stream)
    global_buf.append(char)
    line_buf.append(char)
    if char == '\n':
        _debug_file(''.join(line_buf))
        line_buf = []
    return line_buf


def _poll(stdout, stderr):
    stdoutbuf = []
    stdoutbuf_line = []
    stderrbuf = []
    stderrbuf_line = []
    read_fds = [stdout, stderr]
    x_fds = [stdout, stderr]
    while read_fds:
        rlist, _, _ = select.select(read_fds, [], x_fds)
        if rlist:
            for stream in rlist:
                if stream == stdout:
                    stdoutbuf_line = _process_stream(stream, read_fds, stdoutbuf, stdoutbuf_line)
                if stream == stderr:
                    stderrbuf_line = _process_stream(stream, read_fds, stderrbuf, stderrbuf_line)
    return (''.join(stdoutbuf), ''.join(stderrbuf))


def _run_command(cmd, cwd):
    _log(cwd + ": " + cmd)
    process = subprocess.Popen(cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               shell=True, cwd=cwd)
    (stdout, stderr) = _poll(process.stdout, process.stderr)
    returncode = process.wait()
    if returncode != 0:
        _log("    stdout: " + stdout)
        _log("    stderr: " + stderr)
        _log("    result: " + str(returncode))
    return (stdout, stderr, returncode)


def _get_terraform_env():
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')
    return "TF_VAR_test_id=" + timestamp


def _terraform(action, cwd, terraform_env):
    (stdout, _, returncode) = _run_command(terraform_env + " terraform " + action, cwd)
    if returncode != 0:
        _log("Error running terraform")
        sys.exit(1)
    return stdout


def _get_cluster_ips():
    return os.environ['MASTER_IP'], [os.environ['SLAVE0_IP'], os.environ['SLAVE1_IP']]


def _get_volume_name(terraform_env):
    output = _terraform("output -json", TERRAFORM_DIR, terraform_env)
    jsn = json.loads(output)
    ocid = jsn["volume_ocid"]["value"].split('.')
    return ocid[len(ocid) - 1]


def _scp(instance_ip, src, dest):
    return "scp -o UserKnownHostsFile=/dev/null " + \
           "-o LogLevel=quiet " + \
           "-o StrictHostKeyChecking=no " + \
           "-i " + _get_instance_key_file() + " " + src + " opc@" + instance_ip + ":" + dest


def _ssh(instance_ip, cmd):
    return "ssh -o UserKnownHostsFile=/dev/null " + \
           "-o LogLevel=quiet " + \
           "-o StrictHostKeyChecking=no " + \
           "-i " + _get_instance_key_file() + " opc@" + instance_ip + " " + \
           "\"bash --login -c \'" + cmd + "\'\""


def _create_rc_yaml(volume_name):
    with open("replication-controller.yaml.template", "r") as sources:
        lines = sources.readlines()
    with open("replication-controller.yaml", "w") as sources:
        for line in lines:
            sources.write(re.sub('{{VOLUME_NAME}}', volume_name, line))
    return "replication-controller.yaml"


def _install_driver(instance_ip):
    _run_command(_scp(instance_ip, "../../dist/bin/oci", "/home/opc"), ".")
    _run_command(_ssh(instance_ip, "sudo mkdir -p " + DRIVER_DIR), ".")
    _run_command(_ssh(instance_ip, "sudo cp /home/opc/oci " + DRIVER_DIR), ".")


def _restart_controller(instance_ip):
    (stdout, _, _) = _run_command(_ssh(instance_ip, "docker ps | " + \
                                                    "grep k8s_kube-controller-manager"), ".")
    _run_command(_ssh(instance_ip, "docker stop " + stdout.split()[0]), ".")


def _restart_kubelet(instance_ip):
    _run_command(_ssh(instance_ip, "sudo systemctl restart kubelet.service"), ".")


def _install_oci_creds(instance_ip):
    _run_command(_scp(instance_ip, "flexvolume_driver.json", "/home/opc"), ".")
    _run_command(_scp(instance_ip, _get_oci_api_key_file(), "/home/opc"), ".")
    _run_command(_ssh(instance_ip, "sudo mkdir -p " + DRIVER_DIR), ".")
    _run_command(_ssh(instance_ip, "sudo cp /home/opc/flexvolume_driver.json " + DRIVER_DIR), ".")
    _run_command(_ssh(instance_ip, "sudo cp /home/opc/oci_api_key.pem " + DRIVER_DIR), ".")


def _get_pod_infos(instance_ip):
    (stdout, _, _) = _run_command(_ssh(instance_ip, "kubectl get pods -o wide"), ".")
    infos = []
    for line in stdout.split("\n"):
        line_array = line.split()
        if re.match(r"nginx-controller-.*", line):
            name = line_array[0]
            status = line_array[2]
            node = line_array[6]
            infos.append((name, status, node))
    return infos


def _wait_for_pod_status(instance_ip, desired_status):
    infos = _get_pod_infos(instance_ip)
    num_polls = 0
    while not any(i[1] == desired_status for i in infos):
        for i in infos:
            _log("    - pod: " + i[0] + ", status: " + i[1] + ", node: " + i[2])
        time.sleep(1)
        num_polls += 1
        if num_polls == TIMEOUT:
            for i in infos:
                _log("Error: Pod: " + i[0] + " " + \
                     "failed to achieve status: " + desired_status + "." + \
                     "Final status was: " + i[1])
            sys.exit(1)
        infos = _get_pod_infos(instance_ip)
    return (infos[0][0], infos[0][1], infos[0][2])


def _main():
    _reset_debug_file()
    parser = argparse.ArgumentParser(description='Description of your program')
    parser.add_argument('--no-create',
                        help='Disable the creation of the test volume',
                        action='store_true',
                        default=False)
    parser.add_argument('--no-setup',
                        help='Dont sync the driver + test files to the test instance',
                        action='store_true',
                        default=False)
    parser.add_argument('--no-test',
                        help='Dont run the tests on the test cluster',
                        action='store_true',
                        default=False)
    parser.add_argument('--no-destroy',
                        help='If we are creating the test volume, then dont destroy it',
                        action='store_true',
                        default=False)
    args = vars(parser.parse_args())

    _check_env()
    _create_key_files()

    terraform_env = _get_terraform_env()
    success = True

    _log("Finding the cluster IPs", as_banner=True)
    (master_ip, slave_ips) = _get_cluster_ips()
    _log("Master IP: " + master_ip)
    for slave_ip in slave_ips:
        _log("Slave IP: " + slave_ip)

    if not args['no_create']:
        _log("Creating test volume", as_banner=True)
        _terraform("init", TERRAFORM_DIR, terraform_env)
        _terraform("apply", TERRAFORM_DIR, terraform_env)

    _log("Finding the volume name", as_banner=True)
    volume_name = _get_volume_name(terraform_env)
    _log("Volume Name: " + volume_name)

    if not args['no_setup']:
        _log("Installing flexvolume driver on all of the the nodes", as_banner=True)
        _install_driver(master_ip)
        for slave_ip in slave_ips:
            _install_driver(slave_ip)

        _log("Syncing oci creds to all the nodes", as_banner=True)
        _install_oci_creds(master_ip)
        for slave_ip in slave_ips:
            _install_oci_creds(slave_ip)

        _log("Restarting the controller manager on the master", as_banner=True)
        _restart_controller(master_ip)

        _log("Restarting the kubelet on all the nodes", as_banner=True)
        for slave_ip in slave_ips:
            _restart_kubelet(slave_ip)

        _log("Syncing test resources to the master", as_banner=True)
        _run_command(_scp(master_ip, _create_rc_yaml(volume_name), "/home/opc"), ".")

    if not args['no_test']:
        _log("Running system test: ", as_banner=True)

        _log("Starting the replication controller (creates a single nginx pod).")
        _run_command(_ssh(master_ip, "kubectl create -f replication-controller.yaml"), ".")

        _log("Waiting for the pod to start.")
        (podname1, _, node1) = _wait_for_pod_status(master_ip, "Running")

        _log("Writing a file to the flexvolume mounted in the pod.")
        _run_command(_ssh(master_ip, "kubectl exec " + podname1 + \
                                     " -- touch /usr/share/nginx/html/hello.txt"), ".")

        _log("Does the new file exist?")
        (stdout, _, _) = _run_command(_ssh(master_ip, "kubectl exec " + podname1 + \
                                                      " -- ls /usr/share/nginx/html"), ".")
        if not "hello.txt" in stdout.split("\n"):
            _log("Error: Failed to find file hello.txt in mounted volume")
            sys.exit(1)
        _log("Yes it does!")

        _log("Marking the current node as unschedulable.")
        _run_command(_ssh(master_ip, "kubectl cordon " + node1), ".")

        _log("Deleting the pod. This should cause it to be started on the other node.")
        _run_command(_ssh(master_ip, "kubectl delete pod " + podname1), ".")

        _log("Waiting for the pod to start (on the other node).")
        (podname2, _, node2) = _wait_for_pod_status(master_ip, "Running")

        if node1 == node2:
            _log("Error: Pod failed to appear on the other slave after being deleted/restarted.")
            sys.exit(1)

        _log("Does the new file still exist?")
        (stdout, _, _) = _run_command(_ssh(master_ip, "kubectl exec " + podname2 + \
                                                      " -- ls /usr/share/nginx/html"), ".")
        if not "hello.txt" in stdout.split("\n"):
            _log("Error: Failed to find file hello.txt in mounted volume")
            sys.exit(1)
        _log("Yes it does!")

        _log("Deleteing the replication controller (deletes the single nginx pod).")
        _run_command(_ssh(master_ip, "kubectl delete rc nginx-controller"), ".")

        _log("Adding the origional node back into the cluster.")
        _run_command(_ssh(master_ip, "kubectl uncordon " + node1), ".")

    if not args['no_destroy']:
        _log("Destroying test volume", as_banner=True)
        _terraform("destroy -force", TERRAFORM_DIR, terraform_env)

    _destroy_key_files()

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    _main()
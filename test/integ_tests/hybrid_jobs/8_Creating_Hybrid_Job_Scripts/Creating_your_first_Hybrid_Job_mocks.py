import os
import subprocess
import tarfile
from unittest import mock

default_job_results = ""


def pre_run_inject(mock_utils):
    mocker = mock_utils.Mocker()
    mock_utils.mock_default_device_calls(mocker)
    mock_utils.mock_default_job_calls(mocker)
    mocker.set_log_streams_result({"logStreams": []})
    mocker.set_get_query_results_result(
        {
            "status": "Complete",
            "results": [
                [
                    {"field": "@message", "value": "iteration_number=0;expval=0;"},
                    {"field": "@timestamp", "value": "0"},
                ],
            ],
        },
    )
    mocker.set_start_query_result({"queryId": "TestId"})
    global default_job_results
    default_job_results = mock_utils.read_file("../job_results.json", __file__)
    with open("results.json", "w") as f:
        f.write(default_job_results)
    with tarfile.open("model.tar.gz", "w:gz") as tar:
        tar.add("results.json")
    subprocess.run = subprocess_run
    subprocess.check_output = subprocess_check_output
    subprocess.Popen = subprocess_open

    os.environ["AMZN_BRAKET_DEVICE_ARN"] = (
        f"arn:aws:braket:{mocker.region_name}::device/qpu/arn/TestARN"
    )


def post_run(tb):
    tb.inject(
        """
        import os
        os.remove("model.tar.gz")
        os.remove("results.json")
        """,
    )


def subprocess_run(*args, **kwargs):
    return mock.Mock()


def subprocess_check_output(*args, **kwargs):
    cmd = args[0]
    if cmd[0] == "docker" and cmd[1] == "cp" and cmd[3].startswith("braket-job"):
        local_job_name = cmd[3]
        os.mkdir(local_job_name)
        with open(os.path.join(local_job_name, "results.json"), "w") as f:
            f.write(default_job_results)

    return mock.Mock()


def subprocess_open(*args, **kwargs):
    open_mock = mock.Mock()
    open_mock.stdout.readline.return_value.decode.return_value = "Successfully Tested"
    open_mock.poll.return_value = 0
    return open_mock

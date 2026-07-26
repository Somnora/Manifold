import pytest
from app.templates import JobTemplate, TemplateParameter, VolumeMount, render_template

def test_render_template_basic():
    t = JobTemplate(
        name="test",
        description="A test",
        image="ubuntu",
        command="echo {{param1}}",
        parameters=[TemplateParameter(name="param1", type="string", description="", default=None)]
    )
    res = render_template(t, {"param1": "hello world"}, "fs-test")
    assert res["template_name"] == "test"
    assert "echo 'hello world'" in res["rendered"]
    assert "param1" in res["param_line_mapping"]

def test_render_template_volumes():
    t = JobTemplate(
        name="test-vol",
        description="A test",
        image="ubuntu",
        command="cat /data/file",
        parameters=[TemplateParameter(name="dir", type="string", description="", default=None)],
        volumes=[VolumeMount(host="{persistent}/{{dir}}", container="/data", read_only=True)]
    )
    res = render_template(t, {"dir": "my-dir"}, "fs-test")
    assert "host: /lambda/nfs/fs-test/my-dir" in res["rendered"]
    assert "read_only: true" in res["rendered"]



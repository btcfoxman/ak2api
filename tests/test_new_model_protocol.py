"""Replay the observed protocol with synthetic media; no provider submissions."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.akool_client import AkoolClient, AkoolUpstreamError, FEE_PATH, LIST_PATH
from app.model_catalog import normalize_generation_request
from test_akool_client import settings, upload


def client(owner=None):
    return AkoolClient({"id": owner} if owner else {}, settings())


def media(kind, index=1, seconds=0, mime=None):
    row=upload(kind,index,int(seconds*1000))
    if mime: row.content_type=mime
    row.name="reference.mp3" if kind=="audio" else "reference."+("jpg" if kind=="image" else "mp4")
    return row


def build(model, rows, **values):
    payload=normalize_generation_request({"model":model,"prompt":"synthetic dialogue",**values})
    return client().build_generation_request(payload,rows)


def test_observed_kling_reference_request_keeps_voice_and_omits_unsupported_controls():
    rows=[media("image"),media("video",seconds=6),media("audio",seconds=8.232,mime="audio/mpeg")]
    body=build("kling-4.0",rows,duration=3,resolution="720p",aspect_ratio="21:9",generate_audio=False)
    assert body["model_name"]=="kwaivgi/kling-v4.0/reference-to-video"
    assert body["model_mode"]=="references" and body["video_length"]==3
    assert body["ratio"]=="21:9" and body["resolution"]=="720p"
    assert body["reference_audio_urls"] and {x["type"] for x in body["prompt_info"]}=={"image","video","voice"}
    assert "audio_type" not in body and "generate_audio" not in body
    assert body["extendPrompt"] is False
    # An unsupported sound switch cannot disable Kling's default output sound.
    assert normalize_generation_request({"model":"kling-4.0","prompt":"test","generate_audio":False})["generate_audio"] is True


@pytest.mark.parametrize("seconds,resolution,ratio,voice",[(4,"540p","16:9",False),(3,"720p","1:1",True)])
def test_observed_vidu_requests(seconds,resolution,ratio,voice):
    rows=[media("image")]+([media("audio",seconds=8.232,mime="audio/mpeg")] if voice else [])
    body=build("vidu-q4",rows,duration=seconds,resolution=resolution,aspect_ratio=ratio)
    assert body["model_name"]=="vidu/q4-preview/reference-to-video"
    assert body["model_mode"]=="references" and body["generate_audio"] is True
    assert "videoUrl" not in body and "audio_type" not in body
    assert bool(body.get("reference_audio_urls")) is voice
    assert body["extendPrompt"] is False


@pytest.mark.parametrize("model,seconds",[("kling-4.0",25),("vidu-q4",8.232)])
def test_separate_audio_clips_do_not_share_a_total_duration_cap(model,seconds):
    rows=[media("image")]+[media("audio",i,seconds,"audio/mpeg") for i in range(3)]
    assert len(build(model,rows)["reference_audio_urls"])==3


@pytest.mark.parametrize("model,seconds",[("kling-4.0",2.64),("kling-4.0",0),("kling-4.0",30.001),("vidu-q4",2.64),("vidu-q4",12.001)])
def test_bad_audio_lengths_are_rejected_before_quote_or_submission(model,seconds):
    c=client();c._request=Mock(side_effect=AssertionError("no network expected"))
    with pytest.raises(AkoolUpstreamError,match="Audio reference duration"):
        c.build_generation_request({"model":model,"prompt":"fixture"},[media("image"),media("audio",seconds=seconds,mime="audio/mpeg")])
    c._request.assert_not_called()


def test_vidu_wav_is_not_silently_submitted_as_supported_mp3():
    with pytest.raises(AkoolUpstreamError,match="Audio reference format"):
        build("vidu-q4",[media("image"),media("audio",seconds=8,mime="audio/wav")])


def test_kling_short_video_and_total_long_videos_are_rejected():
    for lengths in ([2.9],[16,16]):
        with pytest.raises(AkoolUpstreamError):
            build("kling-4.0",[media("video",i,x) for i,x in enumerate(lengths)])


@pytest.mark.parametrize("model,expected",[("kling-4.0",False),("vidu-q4",True)])
def test_fee_controls_match_observed_request_without_changing_output_audio(model,expected):
    c=client();rows=[media("image")];body=build(model,rows)
    c._request=Mock(return_value=({"code":1000,"data":{"fee":12}},None))
    quote=c.calculate_fee({"model":model},rows,body)
    assert quote["fee"]==12
    assert c._request.call_args.args==( "POST",FEE_PATH)
    assert c._request.call_args.kwargs["payload"]["options"]["generate_audio"] is expected


@pytest.mark.parametrize("fee",[None,True,-1,float("nan"),float("inf"),"invalid"])
def test_invalid_quotes_cannot_turn_into_free_paid_submissions(fee):
    c=client();c._request=Mock(return_value=({"code":1000,"data":{"fee":fee}},None))
    with pytest.raises(AkoolUpstreamError,match="Fee quote"):
        c.calculate_fee({"model":"vidu-q4"},[],build("vidu-q4",[]))
    assert c._request.call_count==1


def test_explicit_zero_quote_remains_valid():
    c=client();c._request=Mock(return_value=({"code":1000,"data":{"fee":0}},None))
    assert c.calculate_fee({"model":"vidu-q4"},[],build("vidu-q4",[]))["fee"]==0


def test_client_finds_an_older_than_100th_resource_on_next_bounded_round():
    c=client(987654);calls=[]
    def request(method,path,**kwargs):
        assert method=="GET" and path==LIST_PATH
        cursor=kwargs["params"].get("cursor","");calls.append(cursor)
        page=int(cursor.removeprefix("cursor-")) if cursor else 0
        values=[{"_id":"wanted-old-resource","video_status":3,"video":"https://cdn.example.com/result.mp4"}] if page==6 else []
        return {"code":1000,"data":{"result":values,"next_cursor":"cursor-"+str(page+1)}},None
    c._request=request
    first=c.generation_detail("wanted-old-resource")
    assert first["status"]=="PROCESSING" and first["raw"]["lookup"]["has_more"]
    assert calls==["","cursor-1","cursor-2","cursor-3","cursor-4"]
    # A refreshed client still owns the same account/resource scan.
    refreshed=client(987654);refreshed._request=request
    final=refreshed.generation_detail("wanted-old-resource")
    assert final["status"]=="COMPLETE" and final["urls"]
    assert calls[5:]==["","cursor-5","cursor-6"]


@pytest.mark.parametrize("body",[{"code":1003,"msg":"Image upload failed or format not supported. Please re-upload the image."},{"code":1003,"msg":"图片版权审核未通过"}])
def test_definitive_http403_media_or_content_rejection_is_not_login_recovery(body):
    c=client();c.session.request=Mock(return_value=SimpleNamespace(status_code=403,headers={},json=lambda:body,text=""))
    with pytest.raises(AkoolUpstreamError) as caught:c._request("POST","/fixture",payload={},retry=False)
    assert caught.value.code in {"PROVIDER_INVALID_REQUEST","CONTENT_MODERATION_FAILED"}
    assert caught.value.status_code==422
    assert c.session.request.call_count==1


def test_admin_model_omissions_keep_model_specific_defaults():
    import ast
    from pathlib import Path
    from app.schemas import GenerationTaskCreate
    tree=ast.parse((Path(__file__).parents[1]/"app/main.py").read_text(encoding="utf-8"))
    node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=="create_admin_task")
    node.decorator_list=[]
    env={"GenerationTaskCreate":GenerationTaskCreate,"Any":object,"_create":lambda payload:normalize_generation_request(payload)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),"admin_boundary","exec"),env)
    out=env["create_admin_task"](GenerationTaskCreate(model="vidu-q4",prompt="test"))
    assert (out["duration"],out["resolution"],out["aspect_ratio"])==(5,"720p","16:9")


def test_vidu_without_reference_media_does_not_upload_a_black_placeholder(tmp_path,monkeypatch):
    import app.service as service_module
    from app.db import Database
    from test_service import FakeAkoolClient,settings as service_settings
    class NativeVidu(FakeAkoolClient):
        def upload_default_text_video_image(self):
            raise AssertionError("Vidu must keep its zero-reference native contract")
    monkeypatch.setattr(service_module,"AkoolClient",NativeVidu)
    db=Database(str(tmp_path/"vidu.db"));db.upsert_account({"name":"ready","status":"active","last_balance":100})
    db.create_task("vidu-native",{**normalize_generation_request({"model":"vidu-q4","prompt":"test"}),"_estimated_cost":0})
    gateway=service_module.AKService(db,service_settings(tmp_path))
    try:gateway._run_task("vidu-native")
    finally:gateway.stop()
    assert db.get_task("vidu-native")["status"]=="succeeded"


@pytest.mark.parametrize("data,expected,suffix",[(b"RIFFxxxxWAVEdata","audio/wav",".wav"),(b"ID3sample","audio/mpeg",".mp3"),(b"\xff\xfbdata","audio/mpeg",".mp3"),(b"fLaCdata","audio/flac",".flac")])
def test_audio_container_metadata_wins_over_a_misleading_filename(data,expected,suffix):
    from app.akool_client import SIGNATURE_PATH, PROFILE_PATH
    c=client();c._download_source=Mock(return_value=(data,"application/octet-stream","misnamed.mp3"));c._put_signed=Mock()
    def response(method,path,**kwargs):
        if path==SIGNATURE_PATH:
            assert kwargs["payload"]["file_ext"]==suffix
            return {"code":1000,"data":{"upload_url":"https://upload.example.invalid/upload","url":"https://cdn.example.invalid/reference"+suffix,"content_type":expected}},None
        assert path==PROFILE_PATH
        assert kwargs["payload"]["file_name"].endswith(suffix)
        return {"code":1000,"data":{"_id":"profile-audio","duration":8000}},None
    c._request=Mock(side_effect=response)
    result=c.upload_media("https://source.example.invalid/audio","audio")
    assert result.content_type==expected and result.duration_ms==8000 and result.name.endswith(suffix)
    c._put_signed.assert_called_once_with("https://upload.example.invalid/upload",data,expected)


def test_explicit_video_status_zero_is_not_overridden_by_resource_success():
    c=client();c._request=Mock(return_value=({"code":1000,"data":{"result":[{"_id":"pending-resource","video_status":0,"status":3}]}},None))
    assert c.generation_detail("pending-resource")["status"]=="PROCESSING"


@pytest.mark.parametrize("progress",["pending", "nan", "inf", {}, [], False])
def test_invalid_progress_is_a_retryable_query_error_not_a_failed_generation(progress):
    c=client();c._request=Mock(return_value=({"code":1000,"data":{"result":[{"_id":"accepted","video_status":2,"progress":progress}]}},None))
    with pytest.raises(AkoolUpstreamError)as caught:c.generation_detail("accepted")
    assert caught.value.code=="AKOOL_HTTP_ERROR"and caught.value.status_code==502


def test_definitive_http403_reference_download_failure_does_not_trigger_auth_replay():
    c=client();c.session.request=Mock(return_value=SimpleNamespace(status_code=403,headers={},json=lambda:{"code":1003,"msg":"Reference image download failed"},text=""))
    with pytest.raises(AkoolUpstreamError)as caught:c._request("POST","/fixture",payload={},retry=False)
    assert caught.value.code=="MEDIA_DOWNLOAD_FAILED"and caught.value.status_code==422
    assert c.session.request.call_count==1


@pytest.mark.parametrize("model,kind,limit",[("kling-4.0","image",30_000_000),("kling-4.0","video",300_000_000),("kling-4.0","audio",50_000_000),("vidu-q4","audio",50_000_000)])
def test_known_provider_size_limits_reject_before_fee_or_generation(model,kind,limit):
    row=media(kind,seconds=8,mime="audio/mpeg"if kind=="audio"else None)
    row.size=limit
    assert build(model,[row])
    row.size+=1
    with pytest.raises(AkoolUpstreamError)as caught:build(model,[row])
    assert caught.value.code=="PROVIDER_INVALID_REQUEST"and caught.value.status_code==422


def test_audio_signature_with_conflicting_mime_stops_before_put_or_profile():
    from app.akool_client import SIGNATURE_PATH
    c=client();c._download_source=Mock(return_value=(b"RIFFxxxxWAVEdata","audio/mpeg","voice.mp3"));c._put_signed=Mock()
    def response(method,path,**kwargs):
        assert path==SIGNATURE_PATH and kwargs["payload"]["file_ext"]==".wav"
        return {"code":1000,"data":{"upload_url":"https://upload.example.invalid/upload","url":"https://cdn.example.invalid/reference.wav","content_type":"audio/mpeg"}},None
    c._request=Mock(side_effect=response)
    with pytest.raises(AkoolUpstreamError)as caught:c.upload_media("https://source.example.invalid/audio","audio")
    assert caught.value.code=="AKOOL_HTTP_ERROR"
    c._put_signed.assert_not_called()

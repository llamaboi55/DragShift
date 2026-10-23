import threading
import numpy as np
import pytest

from dragshift import audio,video
from dragshift.util import UserError


@pytest.fixture
def sound(tmp_path,media_engines):
    source=tmp_path/'source.wav'
    video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
        'aevalsrc=0.3*sin(2*PI*997*t)|0.2*sin(2*PI*439*t):s=48000:d=2',
        '-c:a','pcm_s16le','-y',str(source)])
    return source


def samples(path):
    return np.frombuffer(video.capture([video.executable('ffmpeg'),'-v','error','-i',str(path),
        '-f','f32le','-c:a','pcm_f32le','pipe:1']),dtype='<f4').reshape(-1,2)


def test_glitch_changes_timbre_and_retains_duration_channels_and_source(sound):
    from dragshift import audio_glitch
    before=sound.read_bytes()
    original=samples(sound)
    low=samples(audio_glitch.export(sound,strength=20,target='WAV')[0])
    high_path=audio_glitch.export(sound,strength=100,target='WAV')[0]
    high=samples(high_path)
    info=audio.probe(high_path)
    assert info.duration==pytest.approx(2,abs=.001) and info.channels==2 and info.sample_rate==48000
    assert high.shape==low.shape==original.shape
    # A gain-only change would leave a small projection residual. Bit crushing
    # and sample hold must introduce a genuinely different waveform.
    gain=np.sum(high*original)/np.sum(original*original)
    assert np.sqrt(np.mean((high-gain*original)**2))>.02
    assert np.sqrt(np.mean((low-original)**2))<np.sqrt(np.mean((high-original)**2))
    assert sound.read_bytes()==before


def test_glitch_zero_and_time_envelope(sound):
    from dragshift import audio_glitch
    original=samples(sound)
    assert np.array_equal(samples(audio_glitch.export(sound,strength=0,target='WAV')[0]),original)
    assert np.array_equal(samples(audio_glitch.export(sound,strength=100,curve=[(0,0),(1,0)],target='WAV')[0]),original)
    result=samples(audio_glitch.export(sound,strength=100,curve=[(0,0),(.3,0),(.5,1),(1,1)],target='WAV')[0])
    assert np.max(np.abs(result[:24000]-original[:24000]))<=1/32768
    assert np.sqrt(np.mean((result[60000:]-original[60000:])**2))>.02


def test_trim_preview_and_validation(sound):
    from dragshift import audio_glitch
    out=audio_glitch.export(sound,start=.4,end=1.8,target='FLAC',preview_seconds=.5)[0]
    assert audio.probe(out).duration==pytest.approx(.5,abs=.002)
    for kwargs in ({'strength':101},{'strength':float('nan')},{'start':1,'end':.1},
                   {'target':'MP4'},{'curve':[(0,0),(.4,1),(.2,1),(1,0)]}):
        with pytest.raises(UserError):
            audio_glitch.export(sound,**kwargs)


def test_cancel_glitch_keeps_sources_and_no_output(sound):
    from dragshift import audio_glitch
    before=set(sound.parent.iterdir())
    cancel=threading.Event()
    cancel.set()
    with video.job_context(video.Control(cancel=cancel)):
        with pytest.raises(video.Cancelled):
            audio_glitch.export(sound)
    assert set(sound.parent.iterdir())==before

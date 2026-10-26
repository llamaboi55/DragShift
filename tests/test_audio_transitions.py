import pytest
from dragshift import audio, video
from dragshift.util import UserError


def test_audio_series_crossfades_in_order_and_preserves_sources(tmp_path,media_engines):
    paths=[]
    for n,freq in enumerate((220,440,880)):
        p=tmp_path/f'{n}.wav'
        video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
                       f'sine=frequency={freq}:duration=2','-y',str(p)])
        paths.append(p)
    before=[p.read_bytes() for p in paths]
    out=audio.transition(paths,duration=.5,target='WAV')[0]
    info=audio.probe(out)
    assert info.duration==pytest.approx(5,abs=.01)
    assert info.channels==2 and info.sample_rate==48000
    import numpy as np
    raw=video.capture([video.executable('ffmpeg'),'-v','error','-i',str(out),'-f','f32le','-ac','1','pipe:1'])
    samples=np.frombuffer(raw,dtype='<f4')
    for t,expected in ((.4,220),(2.4,440),(4.4,880)):
        chunk=samples[int(t*48000):int((t+.25)*48000)]
        frequency=np.argmax(np.abs(np.fft.rfft(chunk)))*48000/len(chunk)
        assert frequency==pytest.approx(expected,abs=5)
    assert [p.read_bytes() for p in paths]==before
    with pytest.raises(UserError):
        audio.transition(paths,duration=1.5)

from types import SimpleNamespace
import pytest
from prism.components.xhs_guest import create_guest_cookie


@pytest.mark.parametrize('status,success,code',[(403,False,300011),(200,False,300011)])
def test_guest_refusal_stops_without_retry_or_echoing_response(status,success,code):
    calls=[]
    class Session:
        def __init__(self,**kwargs):
            assert kwargs['verify'] is True
            self.cookies=kwargs['cookies']
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):
            calls.append(url)
            assert kwargs['allow_redirects'] is False
            return SimpleNamespace(status_code=status,json=lambda:dict(success=success,code=code,msg='token=private'))
    signer=SimpleNamespace(generate_a1=lambda:'device',generate_web_id=lambda a:'web',
        sign_headers_post=lambda **kwargs:{},config=SimpleNamespace(PUBLIC_USERAGENT='agent'))
    with pytest.raises(ValueError) as error:
        create_guest_cookie(signer,Session)
    assert len(calls)==1
    assert 'private' not in str(error.value)


def test_successful_guest_cookie_is_passed_without_a_browser_or_disk(tmp_path):
    class Session:
        def __init__(self,**kwargs):self.cookies=kwargs['cookies']
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):
            assert url=='https://edith.xiaohongshu.com/api/sns/web/v1/login/activate'
            assert kwargs['data']=='{}' and kwargs['timeout']==25
            self.cookies['web_session']='guest'
            return SimpleNamespace(status_code=200,json=lambda:dict(success=True,code=0))
    signer=SimpleNamespace(generate_a1=lambda:'device',generate_web_id=lambda a:'web',
        sign_headers_post=lambda **kwargs:{},config=SimpleNamespace(PUBLIC_USERAGENT='agent'))
    value=create_guest_cookie(signer,Session)
    assert 'web_session=guest' in value
    assert not list(tmp_path.iterdir())

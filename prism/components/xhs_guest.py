"""Create a temporary first-party guest session using the upstream Xhshow API."""


def create_guest_cookie(signer=None, session_factory=None):
    if signer is None:
        from xhshow import Xhshow
        signer=Xhshow()
    if session_factory is None:
        from curl_cffi.requests import Session
        session_factory=Session
    a1=signer.generate_a1()
    cookies={'a1':a1,'webId':signer.generate_web_id(a1)}
    uri='/api/sns/web/v1/login/activate'
    headers=signer.sign_headers_post(uri=uri,cookies=cookies,payload={})
    headers.update({'Origin':'https://www.xiaohongshu.com','Referer':'https://www.xiaohongshu.com/',
        'User-Agent':signer.config.PUBLIC_USERAGENT,'Content-Type':'application/json;charset=UTF-8'})
    with session_factory(impersonate='chrome146',verify=True,cookies=cookies) as session:
        response=session.post('https://edith.xiaohongshu.com'+uri,headers=headers,data='{}',timeout=25,allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f'小红书访客会话未建立（HTTP {response.status_code}），已停止请求。')
        try:
            result=response.json()
        except ValueError as error:
            raise ValueError('小红书访客接口未返回有效会话，已停止请求。') from error
        if result.get('success') is not True or not session.cookies.get('web_session'):
            code=result.get('code')
            code=str(code) if isinstance(code,int) else '未知'
            raise ValueError(f'小红书访客会话未建立（平台状态 {code}），已停止请求。')
        # Only this single first-party request has populated the cookie jar.
        # Values stay in this worker's memory and are never persisted or logged.
        return '; '.join(k+'='+v for k,v in dict(session.cookies).items())

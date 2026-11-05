"""Source failures keep access evidence separate from transport failures."""


class SourceError(ValueError):
    def __init__(self, message, code='source_error', http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class SourceAccessError(SourceError):
    def __init__(self, message, *, code='access_unverified', vip=None, evidence=''):
        if code == 'vip_locked' and (vip is not True or not isinstance(evidence, str) or not evidence.strip()):
            code, vip = 'access_unverified', None
        super().__init__(message, code=code)
        self.access = {'state': code, 'vip': vip, 'evidence': evidence}


class SourceCaptchaError(SourceError):
    def __init__(self, challenge_id, verification=None):
        super().__init__('Sáng Tác Việt yêu cầu xác minh CAPTCHA để tiếp tục.', code='captcha_required')
        self.captcha = verification['captcha'] if verification is not None else {'source': 'stv', 'id': challenge_id}
        self.verification = verification

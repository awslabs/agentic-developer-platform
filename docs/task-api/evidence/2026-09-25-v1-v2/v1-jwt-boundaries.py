import json,time,pathlib
import jwt
from tests.auth.test_cognito_jwt import TestIdTokenAudienceBinding,mock_settings
case=TestIdTokenAudienceBinding();settings=mock_settings.__wrapped__();next(settings)
try:
 validator=case.restricted.__wrapped__(case,None);keys=case.rsa_keypair.__wrapped__(case)
 valid={'token_use':'access','client_id':case.MACHINE_CLIENT};out=[]
 for name,claims in [('valid',valid),('expired',{**valid,'exp':int(time.time())-120}),('wrong_issuer',{**valid,'iss':'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_wrong'}),('wrong_client',{**valid,'client_id':'unregistered-client'}),('wrong_audience_id_token',{'token_use':'id','aud':'another-app'})]:
  token=case._token(validator,keys,**claims)
  try:case._validate(validator,keys,token);accepted=True
  except jwt.InvalidTokenError:accepted=False
  assert accepted==(name=='valid'),name
  out.append({'case':name,'accepted':accepted})
 r={'lane':'Real RS256 signing and production CognitoJWTValidator; JWKS public-key fetch controlled','results':out};pathlib.Path('/home/ubuntu/task-delivery-tmp/isolation/v1-jwt-boundaries.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
finally:
 try:next(settings)
 except StopIteration:pass

# frozen_string_literal: true

# Loaded by Rails after the built-in strategy. The stock provider checks claim
# presence, but does not bind issuer/audience or consume jti. Keep this guard
# alongside the provider through package upgrades; configure-portal-sso.py installs it.
require 'json'
require Rails.root.join('lib/omni_auth/strategies/jwt')
Rails.application.config.filter_parameters += [:jwt]

module AdpJwtValidation
  def decoded
    unless request.params['jwt'].is_a?(String) && request.params['jwt'].bytesize < 10_000
      raise OmniAuth::Strategies::Jwt::ClaimInvalid, 'Missing or oversized ADP login token'
    end
    payload = super
    return payload if @adp_verified

    config = JSON.parse(File.read(Rails.root.join('config/adp-sso.json')))
    now = Time.now.to_i
    unless payload['iss'] == config.fetch('issuer') && payload['aud'] == config.fetch('audience') &&
        payload['sub'].is_a?(String) && !payload['sub'].empty? &&
        payload['iat'].is_a?(Integer) && payload['exp'].is_a?(Integer) &&
        payload['iat'] <= now + 5 && payload['exp'] > now &&
        payload['exp'] - payload['iat'] <= 60 && payload['exp'] > payload['iat'] &&
        payload['jti'].is_a?(String) && payload['jti'].match?(/\A[0-9a-f-]{36}\z/)
      raise OmniAuth::Strategies::Jwt::ClaimInvalid, 'Invalid ADP login claims'
    end

    path = payload['return_path']
    root = config.fetch('relative_root')
    unless path.is_a?(String) && path.start_with?(root + '/') &&
        path.match?(/\A\/[a-zA-Z0-9\/-]*\z/) && !path.include?('//')
      raise OmniAuth::Strategies::Jwt::ClaimInvalid, 'Invalid ADP return destination'
    end

    accepted = Gitlab::Redis::SharedState.with do |redis|
      redis.set("adp:gitlab:sso:#{payload['jti']}", '1', nx: true, ex: 65)
    end
    raise OmniAuth::Strategies::Jwt::ClaimInvalid, 'ADP login already used' unless accepted

    # Devise's supported stored destination. Only the signed gateway path is used.
    session['user_return_to'] = path
    @adp_verified = true
    payload
  end
end

OmniAuth::Strategies::Jwt.prepend(AdpJwtValidation)

# frozen_string_literal: true

# A bearer assertion exchange, deliberately separate from browser OmniAuth.
# ADP signs only after resolving current human/Product membership. GitLab issues
# its own user credential and continues to enforce native repository permissions.
require 'json'
require 'jwt'
require 'openssl'

class AdpCliOauth
  SCOPES = 'api read_user write_repository'

  def initialize(app)
    @app = app
  end

  def call(env)
    config = JSON.parse(File.read(Rails.root.join('config/adp-sso.json')))
    endpoint = '/-/adp/cli/token'
    path = env['SCRIPT_NAME'].to_s + env['PATH_INFO'].to_s
    return @app.call(env) unless path == config.fetch('relative_root') + endpoint
    return response(405, error: 'method_not_allowed') unless env['REQUEST_METHOD'] == 'POST'
    return response(400, error: 'invalid_request') unless env['CONTENT_TYPE'].to_s.split(';').first == 'application/json'

    begin
      body = env.fetch('rack.input').read(16_385)
      raise ArgumentError if body.bytesize > 16_384
      assertion = JSON.parse(body).fetch('assertion')
      raise ArgumentError unless assertion.is_a?(String) && assertion.bytesize < 10_000
      claims = JWT.decode(assertion, OpenSSL::PKey::RSA.new(File.read('/etc/gitlab/adp-jwt-public-key.pem')), true,
        algorithm: 'RS256', verify_iss: true, iss: config.fetch('issuer'),
        verify_aud: true, aud: config.fetch('cli_audience')).first
      validate_claims!(claims)
      accepted = Gitlab::Redis::SharedState.with do |redis|
        redis.set("adp:gitlab:cli:#{claims['jti']}", '1', nx: true, ex: 65)
      end
      raise ArgumentError unless accepted

      user = Identity.find_by(provider: 'jwt', extern_uid: claims.fetch('sub'))&.user
      # The immutable provider identity is authoritative. Usernames may be
      # hashed by the provisioner or retained from an existing OIDC account.
      unless user && user.active? && user.external? && !user.admin? && user.group_members.exists?
        return response(403, error: 'access_denied')
      end
      application = Doorkeeper.config.application_model.find(config.fetch('cli_application_id'))
      raise ArgumentError unless !application.confidential? && application.scopes.to_s == SCOPES
      token = Doorkeeper.config.access_token_model.create_for(application: application, resource_owner: user,
        scopes: SCOPES, expires_in: 7200, use_refresh_token: true, organization_id: application.organization_id)
      response(200, access_token: token.plaintext_token, refresh_token: token.plaintext_refresh_token,
        token_type: 'Bearer', expires_in: token.expires_in, created_at: token.created_at.to_i,
        client_id: application.uid, username: user.username, user_id: user.id, subject: claims.fetch('sub'))
    rescue JWT::DecodeError, ArgumentError, KeyError, JSON::ParserError
      response(401, error: 'invalid_grant')
    end
  end

  def validate_claims!(claims)
    now = Time.now.to_i
    unless claims['purpose'] == 'cli_oauth_bootstrap' &&
        claims['sub'].is_a?(String) && claims['sub'].match?(/\A[0-9a-f-]{36}\z/) &&
        claims['tenant_id'].is_a?(String) && !claims['tenant_id'].empty? &&
        claims['iat'].is_a?(Integer) && claims['exp'].is_a?(Integer) &&
        claims['iat'] <= now + 5 && claims['exp'] > now &&
        claims['exp'] > claims['iat'] && claims['exp'] - claims['iat'] <= 60 &&
        claims['jti'].is_a?(String) && claims['jti'].match?(/\A[0-9a-f-]{36}\z/)
      raise ArgumentError
    end
  end

  def response(status, body)
    [status, { 'content-type' => 'application/json', 'cache-control' => 'no-store', 'pragma' => 'no-cache' }, [JSON.generate(body)]]
  end
end

Rails.application.config.filter_parameters += [:assertion, :refresh_token, :access_token]
Rails.application.config.middleware.use AdpCliOauth

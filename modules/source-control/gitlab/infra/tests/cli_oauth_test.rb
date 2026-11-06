# frozen_string_literal: true

# Run in the disposable Ruby test container, with jwt 2.10.3.
require 'minitest/autorun'
require 'ostruct'
require 'pathname'
require 'fileutils'
require 'stringio'
require 'jwt'
require 'openssl'
require 'json'

module Rails
  def self.root = Pathname.new('/tmp/adp-cli-test')
  def self.application
    @application ||= OpenStruct.new(config: OpenStruct.new(filter_parameters: [], middleware: Object.new.tap { |m| def m.use(*) = nil }))
  end
end

module Gitlab
  module Redis
    module SharedState
      def self.with = yield self
      def self.set(key, value, nx:, ex:)
        @values ||= {}
        return false if @values.key?(key)
        @values[key] = value
      end
      def self.clear = @values = {}
    end
  end
end

class Identity
  class << self
    attr_accessor :user
    def find_by(provider:, extern_uid:)
      raise 'Bad identity resolution' unless provider == 'jwt' && extern_uid == '11111111-2222-4333-8444-555555555555'
      OpenStruct.new(user: user)
    end
  end
end

module Doorkeeper
  def self.config = self
  def self.application_model = self
  def self.access_token_model = self
  def self.find(id)
    raise 'Wrong app' unless id == 42
    OpenStruct.new(confidential?: false, scopes: AdpCliOauth::SCOPES, uid: 'public-client', organization_id: 1)
  end
  def self.create_for(application:, resource_owner:, scopes:, expires_in:, use_refresh_token:, organization_id:)
    raise 'Wrong credential authority' unless resource_owner == Identity.user && scopes == AdpCliOauth::SCOPES &&
      expires_in == 7200 && use_refresh_token && organization_id == 1
    OpenStruct.new(plaintext_token: 'native-token', plaintext_refresh_token: 'native-refresh', expires_in: expires_in, created_at: Time.now)
  end
end

FileUtils.mkdir_p(Rails.root.join('config'))
FileUtils.mkdir_p('/etc/gitlab')
File.write(Rails.root.join('config/adp-sso.json'), JSON.generate(issuer: 'urn:adp:gateway:test',
  cli_audience: 'adp-gitlab-cli-test', relative_root: '/gitlab', cli_application_id: 42))
require_relative '../templates/adp_cli_oauth'

class CliOauthTest < Minitest::Test
  def setup
    @key = OpenSSL::PKey::RSA.new(2048)
    File.write('/etc/gitlab/adp-jwt-public-key.pem', @key.public_key.to_pem)
    @claims = { 'iss' => 'urn:adp:gateway:test', 'aud' => 'adp-gitlab-cli-test', 'purpose' => 'cli_oauth_bootstrap',
      'sub' => '11111111-2222-4333-8444-555555555555', 'tenant_id' => 'example',
      'iat' => Time.now.to_i, 'exp' => Time.now.to_i + 60, 'jti' => '11111111-2222-4333-8444-555555555555' }
    Identity.user = OpenStruct.new(active?: true, external?: true, admin?: false,
      id: 5, username: "migrated-user", group_members: OpenStruct.new(exists?: true))
    Gitlab::Redis::SharedState.clear
    @receiver = AdpCliOauth.new(->(_) { [418, {}, ['other-route']] })
  end

  def exchange(changes = {}, key: @key, path: '/gitlab/-/adp/cli/token', method: 'POST', script_name: '')
    token = JWT.encode(@claims.merge(changes), key, 'RS256')
    @receiver.call('PATH_INFO' => path, 'SCRIPT_NAME' => script_name, 'REQUEST_METHOD' => method,
      'CONTENT_TYPE' => 'application/json', 'rack.input' => StringIO.new(JSON.generate(assertion: token)))
  end

  def test_native_user_oauth_and_one_use_assertion
    status, headers, body = exchange
    assert_equal 200, status
    assert_equal 'no-store', headers['cache-control']
    assert_equal 'native-token', JSON.parse(body.first)['access_token']
    assert_equal @claims['sub'], JSON.parse(body.first)['subject']
    assert_equal 5, JSON.parse(body.first)['user_id']
    assert_equal 'migrated-user', JSON.parse(body.first)['username']
    assert_equal 401, exchange.first
  end

  def test_relative_root_and_other_routes
    assert_equal 418, exchange(path: '/-/adp/cli/token').first
    assert_equal 200, exchange(path: '/-/adp/cli/token', script_name: '/gitlab').first
    assert_equal 405, exchange(method: 'GET').first
  end

  def test_browser_assertion_wrong_signer_and_invalid_lifetimes
    [{ 'aud' => 'adp-gitlab-test' }, { 'iss' => 'other' }, { 'purpose' => 'browser' },
     { 'exp' => Time.now.to_i + 600 }, { 'exp' => Time.now.to_i - 1 }, { 'iat' => Time.now.to_i + 30 },
     { 'sub' => '../admin' }, { 'jti' => 'bad' }].each do |change|
      assert_equal 401, exchange(change).first
    end
    assert_equal 401, exchange({}, key: OpenSSL::PKey::RSA.new(2048)).first
  end

  def test_blocked_admin_unmanaged_and_removed_members_cannot_receive_tokens
    [{ active?: false }, { admin?: true }, { external?: false },
     { group_members: OpenStruct.new(exists?: false) }].each do |change|
      Identity.user = OpenStruct.new(active?: true, external?: true, admin?: false,
        id: 5, username: "migrated-user", group_members: OpenStruct.new(exists?: true))
      change.each { |key, value| Identity.user[key] = value }
      Gitlab::Redis::SharedState.clear
      assert_equal 403, exchange.first
    end
  end
end

# frozen_string_literal: true

# Run in a disposable Ruby image with jwt 2.10.3 (the GitLab 19.1.2 dependency).
require 'minitest/autorun'
require 'jwt'
require 'openssl'
require 'ostruct'
require 'pathname'
require 'tmpdir'
require 'fileutils'
require 'json'

module Rails
  def self.root = Pathname.new('/tmp/adp-jwt-test')
  def self.application = @application ||= OpenStruct.new(config: OpenStruct.new(filter_parameters: []))
end

# The fixture represents the installed strategy's public decoded/session contract.
# Cryptographic verification uses the actual ruby-jwt library, not a decoded mock.
module OmniAuth
  module Strategies
    class Jwt
      ClaimInvalid = Class.new(StandardError)
      attr_reader :request, :session
      def initialize(token, key)
        @request = OpenStruct.new(params: { 'jwt' => token })
        @key = key
        @session = {}
      end
      def decoded = @decoded ||= JWT.decode(request.params['jwt'], @key.public_key, true, algorithm: 'RS256').first
    end
  end
end

module Gitlab
  module Redis
    module SharedState
      def self.with
        yield self
      end
      def self.set(key, value, nx:, ex:)
        @values ||= {}
        return false if nx && @values.key?(key)
        raise 'Unexpected replay window' unless ex == 65
        @values[key] = value
      end
      def self.clear = @values = {}
    end
  end
end

FileUtils.mkdir_p(Rails.root.join('lib/omni_auth/strategies'))
File.write(Rails.root.join('lib/omni_auth/strategies/jwt.rb'), '# fixture loaded above')
FileUtils.mkdir_p(Rails.root.join('config'))
File.write(Rails.root.join('config/adp-sso.json'), JSON.generate(issuer: 'urn:adp:gateway:test', audience: 'adp-gitlab-test', relative_root: '/gitlab'))
require_relative '../templates/adp_jwt_validation'

class PortalSsoTest < Minitest::Test
  def setup
    @key = OpenSSL::PKey::RSA.new(2048)
    @claims = { 'iss' => 'urn:adp:gateway:test', 'aud' => 'adp-gitlab-test', 'sub' => 'canonical-user',
      'iat' => Time.now.to_i, 'exp' => Time.now.to_i + 60, 'jti' => '11111111-2222-4333-8444-555555555555',
      'return_path' => '/gitlab/tenant-example/product-example' }
    Gitlab::Redis::SharedState.clear
  end

  def strategy(changes = {}, key = @key)
    OmniAuth::Strategies::Jwt.new(JWT.encode(@claims.merge(changes), key, 'RS256'), @key)
  end

  def test_valid_login_sets_only_the_signed_product_destination
    login = strategy
    assert_equal 'canonical-user', login.decoded['sub']
    assert_equal @claims['return_path'], login.session['user_return_to']
    assert_equal 'canonical-user', login.decoded['sub'] # info/uid may read twice in one request
  end

  def test_second_request_cannot_replay_a_login
    strategy.decoded
    assert_raises(OmniAuth::Strategies::Jwt::ClaimInvalid) { strategy.decoded }
  end

  def test_wrong_issuer_audience_and_lifetime_are_rejected
    [{ 'iss' => 'other' }, { 'aud' => 'other' }, { 'exp' => Time.now.to_i + 600 },
     { 'iat' => Time.now.to_i + 30 }, { 'jti' => 'invalid' }].each do |changes|
      assert_raises(OmniAuth::Strategies::Jwt::ClaimInvalid) { strategy(changes).decoded }
    end
  end

  def test_external_and_traversal_return_paths_are_rejected
    ['https://attacker.test/', '//attacker.test/', '/gitlab/../admin', '/admin', '/gitlab/a?jwt=secret'].each do |path|
      assert_raises(OmniAuth::Strategies::Jwt::ClaimInvalid) { strategy('return_path' => path).decoded }
    end
  end

  def test_signature_and_expiration_are_verified
    assert_raises(JWT::VerificationError) { strategy({}, OpenSSL::PKey::RSA.new(2048)).decoded }
    assert_raises(JWT::ExpiredSignature) { strategy('exp' => Time.now.to_i - 1).decoded }
  end

  def test_missing_token_fails_closed
    assert_raises(OmniAuth::Strategies::Jwt::ClaimInvalid) { OmniAuth::Strategies::Jwt.new(nil, @key).decoded }
  end
end

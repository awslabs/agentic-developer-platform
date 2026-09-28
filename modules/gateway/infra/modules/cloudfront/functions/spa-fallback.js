// SPA deep-link fallback for the S3 origin — attached to the DEFAULT cache
// behavior only (issue #4386).
//
// Why this is a viewer-REQUEST rewrite and not a response rewrite:
//
// The S3 origin uses OAC without s3:ListBucket, so a GET for a key that does not
// exist answers 403 (Access Denied), not 404. The distribution used to convert
// that 403 into "200 + /index.html" with a distribution-wide
// custom_error_response. That argument cannot be scoped to a behavior or an
// origin, so it also rewrote every genuine 403 the API origin returned —
// authorization denials reached browsers as 200 + SPA HTML, making every
// permission denial invisible to clients, tests, and monitors (#4386).
//
// A response-side fix is not available to a CloudFront Function: the
// viewer-response event cannot modify the status code and has no access to the
// body, so it cannot synthesize "200 + index.html". That would need Lambda@Edge
// on origin-response (extra IAM role, replicated function, per-request cost).
//
// Instead we rewrite the URI *before* the origin fetch, so the object we ask S3
// for is one that exists and the 403 never happens. SPA fallback becomes a
// property of the S3 behavior, and the API behaviors keep their real status
// codes.
//
// Heuristic: a request whose final path segment has no file extension is a
// client-side route (/admin/organizations, /org/123/department/456), so serve
// the app shell. A URI with an extension is a real asset request
// (/assets/index-a1b2c3.js, /cfn-templates/aws_role_v1.yaml) and is passed
// through untouched — a missing asset now surfaces S3's real error instead of a
// 200 HTML page, which is the same reasoning that removed the sibling 404 rule.
function handler(event) {
  var request = event.request;
  var uri = request.uri;

  // Root request: default_root_object handles "/" but be explicit for "" too.
  if (uri === '/' || uri === '') {
    request.uri = '/index.html';
    return request;
  }

  // Last path segment; an extension means "real file, fetch it as asked".
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  if (lastSegment.indexOf('.') === -1) {
    request.uri = '/index.html';
  }

  return request;
}

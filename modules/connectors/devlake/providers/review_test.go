package providers

import (
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
)

type reviewCloseBody struct {
	io.Reader
	closed bool
}

func (b *reviewCloseBody) Close() error { b.closed = true; return nil }

type reviewErrorClient struct {
	plugin.ApiClient
	body *reviewCloseBody
}

func (c reviewErrorClient) Get(string, url.Values, http.Header) (*http.Response, lakeerrors.Error) {
	return &http.Response{StatusCode: 200, Body: c.body}, lakeerrors.Convert(errors.New("fixture transport failure"))
}

func TestReviewTransportFailureClosesReturnedBody(t *testing.T) {
	body := &reviewCloseBody{Reader: strings.NewReader(`{"items":[]}`)}
	result, err := Discover(reviewErrorClient{body: body}, req("argocd", "", 10))
	if err != ErrSource || !body.closed || len(result.Items) != 0 {
		t.Fatalf("transport failure leaked body or scopes: closed=%v, %+v, %v", body.closed, result, err)
	}
}

func TestReviewJenkinsLaterServerFailureIsNotSuccessfulEnd(t *testing.T) {
	request := req("jenkins", "team", 2)
	request.Page = 2
	page, err := Discover(fixture(t, `{"jobs":[]}`, http.StatusInternalServerError), request)
	if err != ErrSource || len(page.Items) != 0 {
		t.Fatalf("upstream later-page 500 became successful empty page: %+v, %v", page, err)
	}
}

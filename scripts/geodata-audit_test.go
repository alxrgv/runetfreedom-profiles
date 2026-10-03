package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/metacubex/mihomo/component/geodata/router"
	"google.golang.org/protobuf/proto"
)

func TestAuditRejectsUnsupportedData(t *testing.T) {
	cases := []struct {
		name      string
		site      *router.GeoSite
		ip        *router.GeoIP
		category  string
		wantError bool
	}{
		{"valid", &router.GeoSite{CountryCode: "TEST", Domain: []*router.Domain{{Type: router.Domain_Full, Value: "example.org"}}}, nil, "test", false},
		{"attribute", &router.GeoSite{CountryCode: "TEST", Domain: []*router.Domain{{Type: router.Domain_Domain, Value: "example.org", Attribute: []*router.Domain_Attribute{{Key: "cn"}}}}}, nil, "test@cn", false},
		{"missing attribute", &router.GeoSite{CountryCode: "TEST", Domain: []*router.Domain{{Type: router.Domain_Full, Value: "example.org"}}}, nil, "test@cn", true},
		{"unknown selector", &router.GeoSite{CountryCode: "TEST", Domain: []*router.Domain{{Type: router.Domain_Type(99), Value: "example.org"}}}, nil, "test", true},
		{"invalid regex", &router.GeoSite{CountryCode: "TEST", Domain: []*router.Domain{{Type: router.Domain_Regex, Value: "["}}}, nil, "test", true},
		{"missing category", &router.GeoSite{CountryCode: "OTHER"}, nil, "test", true},
		{"inverse IP", nil, &router.GeoIP{CountryCode: "PRIVATE", ReverseMatch: true, Cidr: []*router.CIDR{{Ip: []byte{10, 0, 0, 0}, Prefix: 8}}}, "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			directory := t.TempDir()
			sites, ips := &router.GeoSiteList{}, &router.GeoIPList{}
			config := map[string][]string{"geosite": {}, "geoip": {}}
			if tc.site != nil {
				sites.Entry = []*router.GeoSite{tc.site}
				config["geosite"] = []string{tc.category}
			}
			if tc.ip != nil {
				ips.Entry = []*router.GeoIP{tc.ip}
				config["geoip"] = []string{"private"}
			}
			configData, _ := json.Marshal(config)
			siteData, _ := proto.Marshal(sites)
			ipData, _ := proto.Marshal(ips)
			paths := []string{filepath.Join(directory, "config.json"), filepath.Join(directory, "site.dat"), filepath.Join(directory, "ip.dat")}
			for index, data := range [][]byte{configData, siteData, ipData} {
				if err := os.WriteFile(paths[index], data, 0600); err != nil {
					t.Fatal(err)
				}
			}
			previous := os.Args
			os.Args = append([]string{"audit"}, paths...)
			defer func() { os.Args = previous }()
			err := run()
			if (err != nil) != tc.wantError {
				t.Fatalf("unexpected error result: %v", err)
			}
		})
	}
}

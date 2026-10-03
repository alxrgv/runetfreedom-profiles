// Audit only: decode selected entries with upstream generated protobuf types.
// Compile this file inside the pinned converter module; it adds no dependencies.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/metacubex/mihomo/component/geodata/router"
	"github.com/metacubex/mihomo/rules"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) != 4 {
		return fmt.Errorf("usage: geodata-audit CONFIG GEOSITE GEOIP")
	}
	configBytes, err := os.ReadFile(os.Args[1])
	if err != nil {
		return err
	}
	var config struct {
		Geosite []string `json:"geosite"`
		Geoip   []string `json:"geoip"`
	}
	if err = json.Unmarshal(configBytes, &config); err != nil {
		return err
	}
	siteBytes, err := os.ReadFile(os.Args[2])
	if err != nil {
		return err
	}
	ipBytes, err := os.ReadFile(os.Args[3])
	if err != nil {
		return err
	}
	var sites router.GeoSiteList
	var ips router.GeoIPList
	if err = proto.Unmarshal(siteBytes, &sites); err != nil {
		return err
	}
	if err = proto.Unmarshal(ipBytes, &ips); err != nil {
		return err
	}
	result := map[string]map[string]json.RawMessage{"geosite": {}, "geoip": {}}
	for _, name := range config.Geosite {
		parts := strings.SplitN(name, "@", 2)
		var found *router.GeoSite
		for _, entry := range sites.Entry {
			if strings.ToLower(entry.CountryCode) == parts[0] {
				if found != nil {
					return fmt.Errorf("duplicate geosite: %s", name)
				}
				found = entry
			}
		}
		if found == nil {
			return fmt.Errorf("missing geosite: %s", name)
		}
		filtered := proto.Clone(found).(*router.GeoSite)
		if len(parts) == 2 {
			filtered.Domain = nil
			for _, domain := range found.Domain {
				for _, attr := range domain.Attribute {
					if attr.Key == parts[1] {
						filtered.Domain = append(filtered.Domain, domain)
						break
					}
				}
			}
		}
		if len(filtered.Domain) == 0 {
			return fmt.Errorf("empty geosite: %s", name)
		}
		// Unknown fields must not bypass the converter's switch statements.
		if len(filtered.ProtoReflect().GetUnknown()) != 0 {
			return fmt.Errorf("unknown geosite fields: %s", name)
		}
		for _, domain := range filtered.Domain {
			if len(domain.ProtoReflect().GetUnknown()) != 0 {
				return fmt.Errorf("unknown domain fields: %s", name)
			}
			if domain.Type < router.Domain_Plain || domain.Type > router.Domain_Full {
				return fmt.Errorf("unsupported selector: %s", name)
			}
			if domain.Type == router.Domain_Regex {
				if _, err := rules.ParseRule("DOMAIN-REGEX", domain.Value, "DIRECT", nil, nil); err != nil {
					return fmt.Errorf("invalid regex in %s: %w", name, err)
				}
			}
		}
		encoded, err := protojson.Marshal(filtered)
		if err != nil {
			return err
		}
		result["geosite"][name] = encoded
	}
	for _, name := range config.Geoip {
		var found *router.GeoIP
		for _, entry := range ips.Entry {
			if strings.ToLower(entry.CountryCode) == name {
				if found != nil {
					return fmt.Errorf("duplicate geoip: %s", name)
				}
				found = entry
			}
		}
		if found == nil || len(found.Cidr) == 0 {
			return fmt.Errorf("missing or empty geoip: %s", name)
		}
		if found.ReverseMatch {
			return fmt.Errorf("inverse geoip is unsupported: %s", name)
		}
		if len(found.ProtoReflect().GetUnknown()) != 0 {
			return fmt.Errorf("unknown geoip fields: %s", name)
		}
		for _, cidr := range found.Cidr {
			if len(cidr.ProtoReflect().GetUnknown()) != 0 {
				return fmt.Errorf("unknown CIDR fields: %s", name)
			}
		}
		encoded, err := protojson.Marshal(found)
		if err != nil {
			return err
		}
		result["geoip"][name] = encoded
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}

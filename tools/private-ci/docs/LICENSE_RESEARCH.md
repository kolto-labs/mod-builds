# KOTOR licensed files in mod-build CI: license research

Research date: 2026-10-14. Jurisdiction: United States. This is source-based risk analysis, not a legal determination. No Steam/GOG login, game acquisition, redistribution, or provider contact was performed.

## Conclusions

Buying a game establishes access to a licensed copy; it does not by itself authorize every additional copy, automation, cloud deployment, or redistribution. A private checkout inside a public workflow can keep bytes from the public when designed correctly, but privacy and license permission are separate questions. Using only file validation instead of game execution reduces activity but does not eliminate copying and automated-access questions. I found no primary-source license specifically granting third-party KOTOR mod-installation CI on GitHub-hosted runners.

GOG is operationally attractive for offline installers, but current 2026 wording does not establish a more permissive CI license. The clearest route is explicit permission for the described noncommercial testing, or review of the exact purchased edition’s accepted EULA by US counsel. A local owner-controlled runner avoids uploading game copies to GitHub, but is not a universal exception to automation/modification terms.

## Steam account and general terms

[Current Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/), §§1C, 2A, 2G, 4C, 10:

- Account is personal; password/account sharing is restricted, and the subscriber must protect credentials.
- License permits personal, noncommercial use; content is licensed rather than sold. Game-specific subscription terms also apply.
- Copying/modification/derivatives need authorization or an applicable-law exception.
- §4C currently prohibits automation interacting with Steam content/services broadly. Its examples concern account creation, fabricated gameplay, rewards, and adjudication, but are nonexclusive. Do not silently interpret that as only anticheat.
- For US subscribers, Washington law and King County state/federal courts apply.

Short quote (2A): “for your personal, non-commercial use”.

[Official Steamworks uploading documentation](https://partner.steamgames.com/doc/sdk/uploading?l=english) expressly discusses SteamCMD continuous integration and persisted login-token config. This is developer/publisher build documentation, not a grant to consumers to automate copyrighted third-party games. It demonstrates SteamCMD automation is legitimate in some authorized contexts. [Steam Guard documentation](https://help.steampowered.com/en/faqs/view/06B0-26E6-2CF8-254C) requires additional authorization on unrecognized devices. Rotating ephemeral runners complicate unattended login; do not disable account security or bypass ownership checks.

## Actual Steam KOTOR EULA links

[K1 storefront](https://store.steampowered.com/app/32370/) links [K1 EULA](https://store.steampowered.com//eula/32370_eula_0); [TSL storefront](https://store.steampowered.com/app/208580/) links [TSL EULA](https://store.steampowered.com//eula/208580_eula_0). Both served Disney US Terms, updated April 14, 2020. This is stronger evidence than applying a generic modern Aspyr EULA from another game.

Disney §§2A, 2B(ii),(iv),(viii),(x), 3I restrict consumer use to personal/noncommercial purposes, copying, derivative works, business-related use even nonprofit, and automated access/monitoring/copying without written permission. The key short excerpt is “without our express written permission”.

The [current Disney US Terms](https://disneytermsofuse.com/english/), updated May 24, 2024, still restrict those activities and expressly mention scripts and extraction. Exact assent, amendment effectiveness, incorporated supplemental terms, and edition-specific permissions need review. The linked 2020 text and live 2024 text should not be conflated. Disney says product-specific supplemental terms prevail; customer-service representatives cannot modify the agreement. Do not treat a generic support acknowledgment as a formal license amendment.

## Positive, narrower permission for mods

[Aspyr’s official TSL Windows FAQ](https://support.aspyr.com/hc/en-us/articles/4405916441869-Star-Wars-Knights-of-the-Old-Republic-II-The-Sith-Lords-FAQ-Windows), updated July 27, 2021, states: “players can install and use mods for the game”. It also confirms Steam Play access across supported Mac/Linux/Windows platforms. [Linux FAQ](https://support.aspyr.com/hc/en-us/articles/4405905153933-Star-Wars-Knights-of-the-Old-Republic-II-The-Sith-Lords-FAQ-Linux) and [Mac FAQ](https://support.aspyr.com/hc/en-us/articles/205257860-Star-Wars-Knights-of-the-Old-Republic-II-The-Sith-Lords-FAQ-Mac) give similar mod support.

This is meaningful evidence against asserting that all KOTOR mod use is prohibited. It does not specifically authorize sharing originals, third-party cloud copies, unlimited concurrent installations, or CI/gameplay bots. Scope and interaction with broad boilerplate remain uncertain. I did not recover authenticated K1/GOG installer-specific accepted EULAs; do not substitute SWTOR MMO rules or 1998 Jedi Knight “new levels” rules.

## Current GOG terms, not historical versions

[Current GOG User Agreement](https://support.gog.com/hc/en-us/articles/212632089-GOG-User-Agreement) body was obtained from the [official public Zendesk article API](https://support.gog.com/api/v2/help_center/en-us/articles/212632089.json). Rendered page extraction omitted it. Effective date: March 9, 2026; article updated metadata July 7, 2026. Full primary response retained in gog-current.json locally.

§§2.1–2.2: personal license; additional publisher EULAs may apply; GOG agreement prevails on inconsistency. §3.3: account/content cannot be shared/transferred. §4.2: virtual-machine gameplay unsupported, which is not itself a prohibition.

§11.1(a) limits content/services to personal enjoyment; (b) defers derivative-work permission to the content rightsholder. §11.1(e) broadly prohibits software including bots, mods, scripts, and extraction tools interacting with services/content “in any way”. This is in tension with mod-friendly product features and ordinary authorized mods; clarify applicable permission rather than infer that all mods are forbidden or that CI is authorized. §19.3 selects California law and Los Angeles courts for US users; §20.3 lists legal@gog.com for disputes.

Historical “effective until 9 March 2026” articles and the 2014 preview PDF are not current terms. Do not recommend a purchase based on their weaker automation wording.

[GOG’s official DLC installation documentation](https://support.gog.com/hc/en-us/articles/4411524216977-How-do-I-install-my-DLC?product=gog) documents offline backup installers. These reduce recurring account-secret handling and DRM complications, but do not themselves expand copyright rights.

## Copyright copying and encrypted archives

[17 USC §117](https://www.law.cornell.edu/uscode/text/17/117) permits an owner of a program copy to authorize qualifying essential-step or archival copies, with limitations. [US Copyright Office FAQ](https://www.copyright.gov/help/faq/faq-digital.html) emphasizes that archival privilege applies to programs, not every separately copyrighted work, and license terms matter. A game includes code, art, music, and dialogue: do not assume a program-copy rule automatically resolves all assets. An actively used CI baseline is not automatically an archival-only copy. “Purchased” versus “owner of a copy” also needs the case-law analysis.

Inference: Encryption is a confidentiality measure, not an independent copyright exception. Uploading encrypted game archives publicly still creates/retains reproductions and may raise distribution/licensing issues; it should not be presented as legally clean merely because the key stays secret. Private cloud storage likewise does not erase reproduction or agreement questions. Keep authorized originals in owner-controlled private storage; never publish them in releases, Actions caches/artifacts, container registries, or test fixtures. Publish aggregate results and nonreconstructive metadata only. Hashes alone generally reveal far less than bytes, but manifests can contain private paths/account details.

## Recommended paths, in order

1. **Owner-controlled real-game verification.** Public mod-builds workflow orchestrates an isolated self-hosted runner controlled by the purchaser, with an already legitimately installed game. No Steam credentials or originals leave that system. Restrict execution to reviewed immutable revisions and owner-approved events. Separate file-only installation validation from optional gameplay smoke. This is the strongest privacy/operational design, but exact edition terms and automation authority still require review.
2. **Permission-backed hosted lane.** Obtain clear authorization from relevant rightsholder(s) and platform as needed for ephemeral cloud copies, automated mod installation/file validation, concurrency, private storage, and permitted outputs. Then use protected environments, narrowly scoped read-only credentials, pinned code, private temporary storage, no original-file uploads/cache, strict cleanup, and sanitized results. No blanket guarantee that buying one license covers simultaneous runners.
3. **DRM-free edition after review.** GOG offline installers can avoid recurring Steam Guard handling, but review the current GOG terms and the actual edition EULA before purchase. Do not purchase solely on the assumption that DRM-free means unrestricted.
4. **Public compatibility tests plus owner opt-in real lane.** Public CI validates parsers/formats and legally distributable fixtures; licensed real installs run with each adopter’s own files/license. User explicitly prefers actual verification; this is an interim separation, not a claim to have verified original full builds.

## Concrete permission request, ready for user review

“We maintain a noncommercial open-source/public KOTOR mod-build compatibility project. The purchaser alone will authenticate and control execution. May we install our legitimately purchased KOTOR I/II edition into temporary isolated CI environments to run automated file-integrity checks and mod-installation compatibility tests, optionally launch the game for a short private smoke check, and delete all copies after completion? No game code/assets, save files, screenshots, credentials, or reconstructed originals will be published or shared; only aggregate pass/fail metadata will be public. Please confirm permitted hosting locations, concurrent copies/runs per license, required notices, any modding limits, and whether offline private baseline storage is allowed.”

Do not send this without the user’s explicit instruction to contact third parties. Ask for a licensing/business representative able to grant permission; Disney’s terms disclaim customer-service modification authority. Separate author licenses/download terms for the actual mod archives remain necessary.

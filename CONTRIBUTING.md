# Contributing

Thanks for your interest in Memowheel / Trip Video.

## License of contributions

By submitting a contribution (pull request, patch, or otherwise), you agree that
your contribution is licensed under the project's **MIT License** (see `LICENSE`) —
the same terms as the rest of the project. In other words, *inbound = outbound*.

Please only contribute code you have the right to license this way (your own work,
or code already compatible with MIT). Don't paste in code under a copyleft or
non-commercial license.

## Notes for changes

- **No hot reload for Python:** restart the server after editing any `.py` file
  (templates and CSS reload on their own).
- **Bilingual UI:** the interface and generated video are English + Hebrew (RTL).
  Keep both working — add new UI strings to `review_app/i18n.py` in both languages.
- **Design system:** read `DESIGN.md` before any visual or UI change (app *or*
  video). Fonts, colors, and spacing are defined there.

See `README.md` for setup and `PUBLISHING.md` for how releases reach the public
mirror.

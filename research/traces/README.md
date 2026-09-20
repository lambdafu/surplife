01-initialization.log - start the app and connect to device
02-control-brightness.log - lower and increase brightness to minimum and maximum
03-switch-display-off-and-on.log - switch display off and on again
04-upload-animation.log - upload and activate animation (purple eyes)
05-animation-speed.log - control animation speed (decrease to minimum and up again to maximum)
06-second-animation.log - upload a second animation, then go back to the cached first animation from 04-upload-animation.log
07-add-animation-to-carousel.log - add the animation to the carousel on the device (which already had 4 entries)
08-edit-carousel.log - remove animation from carousel (which had 5 entries after 07-add-animation-to-carousel)
09-upload-and-show-image.log - upload and show an image
10-cycle-through-image-animations.log - cycle through all available image animations: left, right, flicker, breath, fall
11-cycle-through-image-animations-back-to-static.log - cycle through all available image animations backwards to static
12-text-AB-red.log - show text "AB" in red statically
13-text-AB-red-scroll.log - show text "AB" in red scrolling right to left
14-graffiti-all-red.log - graffiti solid fill, red (H=0°, S=100%, V=100%)
15-graffiti-all-green.log - graffiti solid fill, green (H≈113°, S=100%, V=100%)
16-graffiti-single-pixel-top-left.log - graffiti single red pixel at top-left (cache hit, no upload)
17-graffiti-single-red-line-left.log - graffiti vertical red line at left edge
18-graffiti-single-red-line-top.log - graffiti horizontal red line at top edge
19-graffiti-all-dark-blue.log - graffiti solid fill, dark blue (H≈203°, S≈67%, V≈35%)
20-graffiti-all-white.log - graffiti solid fill, white (S=0%, V=100%)
21-graffiti-all-black.log - graffiti solid fill, black (S=0%, V≈3%)
22-graffiti-all-purple.log - graffiti solid fill, purple (H≈264°, S≈73%, V≈32%)
23-graffiti-all-pink.log - graffiti solid fill, pink max S/V (H≈281°, S=100%, V=100%)
24-graffiti-all-pink-25s.log - pink, S slider at ~25% (decoded S≈73%)
25-graffiti-all-pink-50s.log - pink, S slider at ~50% (decoded S≈53%)
26-graffiti-all-pink-75s.log - pink, S slider at ~75% (decoded S≈27%)
27-graffiti-all-pink-95s.log - pink, S slider at ~95% (decoded S≈7%)
28-graffiti-all-pink-25v.log - pink, V slider at ~25% (decoded V≈68%)
29-graffiti-all-pink-50v.log - pink, V slider at ~50% (decoded V≈52%)
30-graffiti-all-pink-75v.log - pink, V slider at ~75% (decoded V≈29%)
31-graffiti-all-pink-95v.log - pink, V slider at ~95% (decoded V≈3%)
32-graffiti-pixels-5,0.log - graffiti single red pixel at (5,0)
33-graffiti-pixels-0,3.log - graffiti single red pixel at (0,3)
34-graffiti-pixels-5,3.log - graffiti single red pixel at (5,3)
35-graffiti-pixels-95,15.log - graffiti single red pixel at (95,15), bottom-right corner
36-graffiti-pixels-5,3-9,5.log - graffiti two red pixels at (5,3) and (9,5)
37-graffiti-pixels-5,3-15,13-90,5.log - graffiti three red pixels at (5,3), (15,13), and (90,5)
38-graffiti-pixels-5,3-6,3.log - graffiti two red pixels at (5,3) and (6,3), horizontally adjacent
39-graffiti-pixels-5,3-5,4.log - graffiti two red pixels at (5,3) and (5,4), vertically adjacent
40-graffiti-pixels-5,1.log - graffiti single red pixel at (5,1)
41-graffiti-pixels-5,2.log - graffiti single red pixel at (5,2)
42-graffiti-pixels-red-5,3-green-9,5.log - graffiti red pixel at (5,3) + bright green pixel at (9,5)
43-graffiti-pixels-red-5,3-midgreen-9,5.log - graffiti red pixel at (5,3) + mid-green (S≈50%, V≈50%) pixel at (9,5)
44-graffiti-pixels-red-1,0.log - graffiti single red pixel at (1,0)
45-graffiti-pixels-red-2,0.log - graffiti single red pixel at (2,0)
46-graffiti-pixels-red-8,0.log - graffiti single red pixel at (8,0)
47-graffiti-pixels-red-9,0.log - graffiti single red pixel at (9,0)
48-graffiti-pixels-red-95,14.log - graffiti single red pixel at (95,14)
49-graffiti-pixels-red-95,13.log - graffiti single red pixel at (95,13)
50-graffiti-pixels-red-95,12.log - graffiti single red pixel at (95,12)
51-graffiti-pixels-red-94,15.log - graffiti single red pixel at (94,15)
52-graffiti-pixels-red-47,10.log - graffiti single red pixel at (47,10) — encoding transition zone
53-graffiti-pixels-red-47,9.log - graffiti single red pixel at (47,9) — last position before encoding change
54-graffiti-pixels-red-47,11.log - graffiti single red pixel at (47,11)
55-graffiti-pixels-red-47,12.log - graffiti single red pixel at (47,12)
56-graffiti-pixels-red-47,13.log - graffiti single red pixel at (47,13)
57-graffiti-pixels-red-47,14.log - graffiti single red pixel at (47,14)
58-graffiti-pixels-red-47,15.log - graffiti single red pixel at (47,15)
59-graffiti-pixels-red-48,0.log - graffiti single red pixel at (48,0)
60-graffiti-draw-red-0,0-1,0-2,0-0,15-95,0-95,15.log - live graffiti drawing 6 red pixels via ea 11 direct draw
